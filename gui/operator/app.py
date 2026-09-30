"""The Operator Console application.

This module assembles the console: one state bus, one session controller, one
window, the workspace tabs this build offers, a header and a footer.  It is
also the module the boundary gate walks, so it is deliberately thin -
assembly and routing, no domain logic.

The routing rule is the whole design in one sentence:

    a widget raises an *action*, the console decides what confirmation it needs
    and runs it on a worker, the worker publishes to the bus, and the Tk thread
    drains the bus and repaints.

Nothing shortcuts that path.  There is no place in this file where a button
press performs I/O, and no place where a worker touches a widget.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Tuple

from . import status as st
from . import tokens
from .i18n import LanguageSwitch
from .theming import ThemeSwitch
from .session.controller import SessionController, SessionError, utc_now
from .session.profile import ExperimentProfile, ProfileError, default_profile
from .shell.cockpit_header import CockpitHeaderBar
from .shell.confirm import ConfirmationOutcome, evaluate_confirmation, spec_for
from .shell.footer import FooterBar
from .shell.header import HeaderBar
from .shell.window import ConsoleWindow
from .sources import batch as batch_source
from .sources import cockpit as cockpit_source
from .viewmodel.bus import StateBus
from .viewmodel.types import SessionState
from .workspaces import WORKSPACE_MODULES, load_workspace

logger = logging.getLogger("gui.operator.app")

#: The console never claims LIVE without evidence for it, and it never claims
#: REPLAY before a recording has been opened either.  It starts DISCONNECTED:
#: nothing contacted, nothing observed, nothing to mislead anyone with.  The
#: operator then chooses Live or Replay explicitly, and each choice has to bring
#: its own evidence before a session may start.
DEFAULT_MODE: str = "DISCONNECTED"

#: The two session modes an operator may ask for.  Anything else is a mode the
#: source decides (SYNTHETIC/EMULATED belong to the offline runner), and no
#: control in this console sets one.
SELECTABLE_MODES: Tuple[str, ...] = ("LIVE", "REPLAY")

#: A deployment that publishes loopback HTTP endpoints through its pinned vector
#: is a development stack.  Binding one is an operator act, declared in the
#: environment, never a code default - the same pattern as the experiment
#: runner's AIC_ENV_DRIVER_APPROVED gate.  Without it, Bind uses only what the
#: integration-values document itself states.
LOOPBACK_DEPLOYMENT_ENV: str = "AIC_LOOPBACK_DEPLOYMENT_APPROVED"


class OperatorConsole:
    """The integrated Operator Console.

    Constructible without a display: ``__init__`` builds the bus, the controller
    and the workspace objects, and ``create_window`` is the first call that
    touches the toolkit.  That split is what lets the ten-step scenario drive
    the console headlessly.
    """

    def __init__(self, *, profile: Optional[ExperimentProfile] = None,
                 runs_root: Optional[str] = None,
                 theme: str = tokens.DEFAULT_THEME,
                 llm_manager: Any = None,
                 legacy_episode: Any = None,
                 r1_client: Any = None,
                 capability_manifest: Optional[Mapping[str, Any]] = None,
                 bus: Optional[StateBus] = None,
                 controller: Optional[SessionController] = None,
                 title: str = "Agentic Intent Coordinator - Operator Console"
                 ) -> None:
        self.bus = bus or StateBus()
        self.theme = theme
        #: The advisory proposer inventory, or ``None``.  Advisory is the whole
        #: of it: the Contract Studio's Intent Agent is deterministic and makes
        #: no model call, so this list informs nothing the Kernel decides.  It
        #: is injected because enumerating it probes providers, and a console
        #: does not get to decide to do that.
        self.llm_manager = llm_manager
        #: The preserved pre-Kernel episode runtime, or ``None``.  ``None`` is
        #: the deployed build: the console imports no part of that runtime and
        #: cannot construct one, so every legacy-episode action refuses by name
        #: rather than by checking a flag.  Only
        #: ``tools.legacy.episode_support`` implements this port, and only an
        #: explicitly opt-in entry hands it over -- the same composition rule
        #: as :meth:`attach_kernel_session`.
        self.legacy_episode = legacy_episode
        self.r1_client = r1_client
        self.capability_manifest = dict(capability_manifest or {})
        self.profile = profile or default_profile()
        if runs_root:
            self.profile = self.profile.with_overrides(runs_root=runs_root)
        self.controller = controller or SessionController(self.bus,
                                                          profile=self.profile)
        self.window = ConsoleWindow(
            self.bus, title=title, theme=theme,
            drain_interval_ms=self.profile.refresh_interval_ms)
        self.header = HeaderBar(theme=theme)
        #: Task section 9's seven always-visible Cockpit items, and the one
        #: control that must be reachable from every workspace.  A second row
        #: rather than a replacement: the phase-B header answers "what is this
        #: console doing", this one answers "what is the Kernel doing", and
        #: collapsing them would drop one of the two.
        self.cockpit_header = CockpitHeaderBar(theme=theme,
                                               on_action=self.handle_action)
        self.footer = FooterBar(theme=theme, on_action=self.handle_action)
        #: Display language only; attaches its corner control in
        #: ``create_window``.  Headless consoles keep the default (English).
        self.language = LanguageSwitch(theme=theme)
        #: Display theme only, beside the language box and under the same rule:
        #: it repaints widgets and never changes a value, a status or a record.
        #: ``authored`` is the theme every widget is built in, so the tick hook
        #: can convert anything a workspace creates after the switch.
        self.theme_switch = ThemeSwitch(theme=theme)
        #: Set by track T2 when the intent workspace lands; until then a submit
        #: is refused with a stated reason rather than silently dropped.
        self.intent_submitter: Optional[Callable[[str], Any]] = None
        #: Injected by tests and by the headless scenario so the confirmation
        #: flow is exercised without a display.
        self.confirm: Optional[Callable[[Any], ConfirmationOutcome]] = None
        #: Identity of the recorded source this console is reading, or ``None``.
        #: Set only by :meth:`attach_replay_source`; it is the evidence that
        #: backs a REPLAY session and it can never be typed in.
        self.replay_source: Optional[Mapping[str, Any]] = None
        #: The Kernel submission session the Contract Studio drives, or
        #: ``None``.  Set only by :meth:`attach_kernel_session`: a console
        #: cannot build one, because building one means choosing a deployment.
        self.kernel_session: Optional[Any] = None
        #: The last Cockpit projection published, or ``None`` before the first.
        #: Held so the header can repaint every tick without re-reading the
        #: Kernel: the projection is the Kernel's, the repaint is the console's.
        self.cockpit: Optional[Any] = None
        #: The Batch Experiments session, or ``None``.  Built lazily on the
        #: first batch action, because it needs the runs root and the Kernel
        #: session factory and neither is known at construction time.
        self.batch: Optional[Any] = None
        #: How a batch case gets a Kernel session.  Set by composition through
        #: :meth:`attach_batch_sessions`; without it Start Batch is refused by
        #: name rather than falling back to a second decision path.
        self.batch_session_factory: Optional[Callable[[Any], Any]] = None
        #: The Live composition, once a deployment has been named and accepted.
        #: ``None`` means this console has no deployment and cannot go Live -
        #: which is the state it starts in and returns to on Disconnect.
        self.live: Optional[Any] = None
        #: What the operator asked for.  ``None`` until they choose, and the
        #: choice is checked against the evidence rather than believed.
        self.requested_mode: Optional[str] = None
        self._state = SessionState(mode=DEFAULT_MODE)
        #: workspace id -> the state object last painted into it.
        self._painted: dict = {}
        self._workspaces = self._build_workspaces()
        for workspace in self._workspaces:
            self.window.add_workspace(workspace)
        self.bus.subscribe("session", self._on_session)
        # A profile handed to the constructor names its manifest too; adopting
        # it here means the two ways in - constructor and Load - behave the same.
        if not self.capability_manifest:
            self._load_profile_capability(self.profile)

    # -- assembly ----------------------------------------------------------- #

    def _build_workspaces(self) -> Tuple[Any, ...]:
        """Construct the panes, each with the callbacks it actually has.

        ``load_workspace`` retries bare when a workspace does not accept the
        console's keywords, which kept four parallel tracks unblocked - but it
        also meant the Intent & Decision pane was built with *no* commands at
        all in an assembled console: Preview, Submit, Withdraw, Refresh and the
        model selector were live widgets wired to nothing.  Each pane is now
        offered the keywords it declares, so a dropped callback is a build
        error rather than a silently inert button.
        """
        return tuple(
            load_workspace(workspace_id, module_name, class_name, title,
                           **self._workspace_kwargs(workspace_id))
            for workspace_id, module_name, class_name, title
            in WORKSPACE_MODULES)

    def _workspace_kwargs(self, workspace_id: str) -> dict:
        common = {"theme": self.theme}
        if workspace_id == "main":
            # The same two callbacks Intent & Decision uses, so a submit or a
            # role-model choice made on the Main pane is the identical action
            # and lands in the identical file -- there is no second path.
            return {**common,
                    "on_submit": self._workspace_submit,
                    "on_role_models": lambda chosen: self.controller.run_in_worker(
                        "agent-models", lambda: self._workspace_role_models(chosen)),
                    "role_models": self._load_role_models()}
        if workspace_id == "intent_decision":
            return {"bus": self.bus,
                    "on_preview": self._workspace_preview,
                    "on_submit": self._workspace_submit,
                    "on_withdraw": self._workspace_withdraw,
                    "on_model_switch": lambda name:
                        self.handle_action("llm_select", str(name)),
                    "on_model_refresh": lambda:
                        self.handle_action("llm_refresh", ""),
                    "on_role_models": lambda chosen: self.controller.run_in_worker(
                        "agent-models", lambda: self._workspace_role_models(chosen)),
                    "role_models": self._load_role_models(),
                    "intent_set_path": str(Path(self.profile.runs_root) / "agent-intent-set.json"),
                    "on_run_sitting": self._workspace_run_sitting,
                    "on_stop_sitting": self._workspace_stop_sitting}
        if workspace_id == "analysis":
            return {**common,
                    "on_export": lambda figure_id:
                        self.handle_action("export", str(figure_id or "")),
                    "on_load_run": lambda path:
                        self.handle_action("load_run", str(path or ""))}
        if workspace_id == "live_ops":
            return {**common, "controller": self.controller,
                    "on_action": self.handle_action}
        if workspace_id == "contract_studio":
            return {**common, "bus": self.bus, "on_action": self.handle_action}
        if workspace_id in ("trial_safety", "evidence_ledger"):
            # Bus only.  Both panes are read-only by construction (task
            # section 9.9): there is no action to route because there is no
            # control to raise one.
            return {**common, "bus": self.bus}
        if workspace_id == "batch_experiments":
            return {**common, "bus": self.bus,
                    "on_action": self.handle_action,
                    "on_edit": lambda key, value:
                        self.handle_action("batch_edit",
                                           f"{key}={value}")}
        if workspace_id == "objective_registry":
            # Theme only.  This pane has no action to route and no source to
            # read: it draws the objective registry, which is a literal.
            return {**common}
        return {}

    # -- workspace callbacks ------------------------------------------------- #

    def _workspace_text(self, payload: Any) -> str:
        """Read the intent text out of whatever the form handed us."""
        if isinstance(payload, Mapping):
            for key in ("intentText", "intent_text", "text"):
                value = payload.get(key)
                if value:
                    return str(value)
            return ""
        return str(payload or "")

    def _workspace_preview(self, payload: Any) -> None:
        self.handle_action("preview", self._workspace_text(payload))

    def _workspace_submit(self, payload: Any) -> None:
        self.handle_action("submit", self._workspace_text(payload))

    def _workspace_withdraw(self, row: Any) -> None:
        self.handle_action("withdraw", str(getattr(row, "intent_id", "") or ""))

    @property
    def workspaces(self) -> Tuple[Any, ...]:
        return self._workspaces

    def workspace(self, workspace_id: str) -> Optional[Any]:
        return next((w for w in self._workspaces if w.id == workspace_id), None)

    def show_run(self, store: Any, *, primary: bool = True) -> Any:
        """Load a stored run into the Analysis workspace.  **Worker thread.**

        Added at integration.  ``AnalysisWorkspace`` renders its ``model`` and
        nothing else - deliberately, so the Tk thread does no I/O - and
        ``load_run_view`` is the worker-thread function that fills it.  No track
        owned the call between them, so the Analysis tab of an assembled console
        was an empty panel however many runs the operator had recorded.

        A metric is pre-selected only when the run's own index says one is OK,
        so the chart never opens on a metric the run did not measure.
        """
        from .workspaces.analysis import load_run_view

        view = load_run_view(store)
        workspace = self.workspace("analysis")
        model = getattr(workspace, "model", None)
        if model is None:
            return view
        model.add_run(view, primary=primary)
        if model.selection.metric is None:
            measured = [name for name, entry in sorted(view.metric_index.items())
                        if isinstance(entry, Mapping)
                        and entry.get("status") == "OK"]
            if measured:
                model.selection.metric = measured[0]
        # A new state object is what makes the repaint loop treat the pane as
        # dirty; the model changed underneath it and identity comparison alone
        # would never notice.
        self.controller.publish_state()
        return view

    @property
    def state(self) -> SessionState:
        return self._state

    def create_window(self):
        """Build the real window.  Requires a display."""
        root = self.window.create(header=self.header, footer=self.footer,
                                  cockpit_header=self.cockpit_header)
        self.confirm = self._ask_confirmation
        self.window.add_tick_hook(self._repaint)
        self.language.attach(self.window)
        self.window.add_tick_hook(self.language.refresh)
        self.theme_switch.attach(self.window)
        self.window.add_tick_hook(self.theme_switch.refresh)
        self.controller.publish_state()
        return root

    def run(self) -> None:
        if self.window.root is None:
            self.create_window()
        try:
            self.window.run()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Finalize an open run rather than abandoning it.

        A console that exits while recording would leave a run directory with no
        manifest.  That case is already handled - it re-opens as INTERRUPTED -
        but leaving it deliberately when we know we are closing would be
        throwing away a disposition we could have recorded honestly.
        """
        if self.controller.disposition == "RUNNING":
            try:
                self.controller.stop("INTERRUPTED")
            except Exception:
                logger.exception("interrupted finalize failed")
        self.save_gui_state()

    # -- bus -> render ------------------------------------------------------ #

    def _on_session(self, state: Any) -> None:
        if isinstance(state, SessionState):
            self._state = state

    def _repaint(self) -> None:
        """Tk thread, once per drain.  Formatting only.

        Two deliberate economies, both of which matter at 10 Hz across five
        workspaces:

        * the header and footer repaint every tick, because the elapsed time and
          the UTC clock have to move;
        * a workspace repaints only when it is **visible** and only when the
          state object has actually changed.  The state is a frozen snapshot
          republished on change, so identity is a sound and free comparison, and
          the hidden panes' tables are not rebuilt behind the notebook.

        A workspace that becomes visible repaints on the next tick, which is one
        drain interval away.
        """
        state = self._state
        stats = self.bus.stats()
        self.header.update(state, bus_stats=stats)
        self.cockpit_header.update(self.cockpit_view())
        self.footer.update(state, bus_stats=stats)
        active = self.window.active_workspace
        for workspace in self._workspaces:
            if active is not None and workspace.id != active:
                continue
            if (self._painted.get(workspace.id) is state
                    and state is not None):
                continue
            try:
                workspace.on_state(state)
                self._painted[workspace.id] = state
            except Exception:
                logger.exception("workspace %s repaint failed",
                                 getattr(workspace, "id", "?"))

    # -- actions ------------------------------------------------------------ #

    def handle_action(self, key: str, payload: str = "") -> None:
        """Route one operator action.  Called on the Tk thread; returns at once.

        Every branch either refuses with a stated reason or hands the work to a
        worker.  Nothing blocks here, because this runs on the thread that
        repaints.
        """
        handler = self._handlers().get(key)
        if handler is None:
            self._warn(f"unknown action {key!r}")
            return
        try:
            handler(payload)
        except SessionError as exc:
            self._warn(str(exc))
        except Exception as exc:                          # pragma: no cover
            logger.exception("action %s failed", key)
            self._warn(f"{type(exc).__name__}: {exc}")

    def _handlers(self) -> dict:
        """The routing table, in one place.

        Exposed through :meth:`action_keys` so a reachability test can assert
        that every control the window offers is routed here, instead of the two
        drifting apart until a button raises an action nobody handles.
        """
        return {
            "preflight": self._action_preflight,
            "start": self._action_start,
            "stop": self._action_stop,
            "abort": self._action_abort,
            "submit": self._action_submit,
            "preview": self._action_preview,
            "withdraw": self._action_withdraw,
            "llm_refresh": self._action_llm_refresh,
            "llm_select": self._action_llm_select,
            "bind_live": self._action_bind_live,
            "load_replay": self._action_load_replay,
            "mode_live": self._action_mode_live,
            "mode_replay": self._action_mode_replay,
            "disconnect": self._action_disconnect,
            "load_run": self._action_load_run,
            "export": self._action_export,
            "profile_new": self._action_profile_new,
            "profile_load": self._action_profile_load,
            "profile_save": self._action_profile_save,
            "kernel_draft": self._action_kernel_draft,
            "kernel_confirm": self._action_kernel_confirm,
            "kernel_start": self._action_kernel_start,
            "kernel_abort": self._action_kernel_abort,
            "kernel_estop": self._action_kernel_estop,
            "batch_edit": self._action_batch_edit,
            "batch_confirm": self._action_batch_confirm,
            "batch_start": self._action_batch_start,
        }

    def action_keys(self) -> Tuple[str, ...]:
        """Every action this console routes."""
        return tuple(sorted(self._handlers()))

    def _warn(self, message: str, *, kind: str = "ACTION_REFUSED") -> None:
        self.controller.record_event(lane="WARNING", kind=kind,
                                     severity="WARNING", title=message)

    def _action_preflight(self, _payload: str) -> None:
        def _run() -> None:
            self.controller.preflight(**self.preflight_inputs())
            self.refresh_status()
            self.refresh_backends(discover=False)

        self.controller.run_in_worker("preflight", _run)

    def preflight_inputs(self) -> dict:
        """What Preflight reads, from the deployment when there is one.

        With a Live composition attached the R1 rows are checked against the
        real transport and the capability manifest is the digest-pinned one the
        integration document names.  Without it the same checks run and report
        Unsupported with their reason - which is the difference between "no
        deployment" and "a deployment that failed", and an operator must be able
        to tell those apart.
        """
        if self.live is not None:
            inputs = self.live.preflight_kwargs(runs_root=self.profile.runs_root)
            return {key: value for key, value in inputs.items()
                    if value is not None or key == "runs_root"}
        return {"r1_client": self.r1_client,
                "capability_manifest": self.capability_manifest or None,
                "llm_backend_names": self._backend_names(),
                "runs_root": self.profile.runs_root}

    def refresh_status(self, *, readiness_ladder: Optional[Mapping[str, Any]] = None
                       ) -> None:
        """Project what is known into the topology grid and readiness strip.

        Worker-thread function.  Added at integration: ``build_topology`` and
        ``build_readiness`` are pure projections that T1 wrote and tested, and
        ``SessionController.set_components`` was waiting for them, but no track
        owned the line between the two - so an assembled console showed an empty
        topology and "no readiness evidence" no matter what was attached.

        Everything here is projected from what the console already holds: the
        declared capability manifest, the rApp's own durable state and FSM
        snapshot when a client is attached, and the readiness ladder the
        recorded source carries.  Nothing opens a connection and nothing is
        invented - an element the manifest does not declare renders Unsupported,
        and an absent ladder stays absent rather than defaulting to green.
        """
        from oran.rapp import status_projection as projection

        from .workspaces.live_ops import build_readiness, build_topology

        manifest = self.capability_manifest or {}
        client = self.r1_client
        if self.live is not None:
            manifest = dict(self.live.integration.capability_manifest)
            client = self.live.integration.r1_client()
        capability = projection.project_capability(manifest)
        rapp_state = (projection.project_state_store(client)
                      if client is not None else None)
        # The console owns no decision runtime, so it reads the FSM snapshot
        # off whatever a *bound* composition holds.  On the deployed build
        # nothing is bound to a legacy adapter and the row says so, rather than
        # a console-owned coordinator making the row green on its own.
        adapter = (getattr(self.live, "coordinator", None)
                   if self.live is not None else None)
        transitions = (projection.project_transitions(adapter)
                       if adapter is not None else ())
        components = build_topology(
            capability, rapp_state=rapp_state, transitions=transitions,
            coordinator_present=adapter is not None)
        readiness = build_readiness(rapp_state, ladder=readiness_ladder)
        self.controller.set_components(components, readiness)
        # The grid carries each element's own observation time, source and
        # freshness; this records the *session-level* facts a reader needs to
        # interpret them - which mode produced them, from which deployment, and
        # when the projection ran.
        stale = [c.element_id for c in components
                 if (c.freshness or "").upper() in ("STALE", "AGING")]
        self.controller.record_event(
            lane="SESSION", kind="STATUS_PROJECTED", origin="OBSERVED",
            title=f"{len(components)} element(s) projected",
            detail={"mode": self.controller.mode,
                    "observedAt": utc_now(),
                    "source": ("LIVE_INTEGRATION" if self.live is not None
                               else "ATTACHED_CLIENT" if client is not None
                               else "CAPABILITY_MANIFEST_ONLY"),
                    "integration": (self.live.identity() if self.live is not None
                                    else {}),
                    "staleElements": stale,
                    "readinessSegments": [s.segment_id for s in readiness]})

    def refresh_backends(self, *, discover: bool = False) -> Tuple[Any, ...]:
        """Publish the proposer inventory.  Worker-thread function.

        ``discover`` re-runs the existing manager-owned discovery (the LiteLLM /
        local-server path); without it this is a pure projection of what the
        manager already holds, which is what Preflight wants.
        """
        composition = self.live
        if composition is None:
            manager = self.llm_manager
            if manager is None:
                return ()
            from .sources.llm_registry import LlmRegistry

            views = LlmRegistry(manager).views()
            active = None
            try:
                active = manager.active_backend_name()
            except Exception:
                active = None
            self.controller.set_llm_backends(views, active)
            return views
        if discover:
            composition.refresh_backends()
        views = composition.backend_views()
        self.controller.set_llm_backends(views, composition.active_backend())
        return views

    def attach_replay_source(self, path: Any, *,
                             session_id: Optional[str] = None) -> Any:
        """Load a recorded source and make it this console's session source.

        Worker-thread function - it reads a directory and writes a run.  It
        returns the finalized :class:`SessionStore` the adapter produced, and
        remembers that run's identity as the evidence for the next REPLAY
        session.  ``mode`` is never taken from here: the adapter decides it from
        the source, and this console only records which source it was.

        Any bound deployment is dropped, symmetrically with
        :meth:`attach_live_integration` dropping a recorded source.  A console
        holding both could start a session whose evidence named one of them and
        whose data came from the other.
        """
        from .sources import replay as replay_sources

        if self.live is not None:
            self.live = None
            self.intent_submitter = None
            self.requested_mode = None
            self.controller.record_event(
                lane="SESSION", kind="LIVE_INTEGRATION_DETACHED",
                title="a recorded source was attached",
                detail={"meaning": "one session source at a time; the "
                                   "deployment binding was dropped so a Replay "
                                   "cannot be mistaken for a Live run"})

        store = replay_sources.load(
            path, self.profile.runs_root, session_id=session_id,
            capability_manifest=self.capability_manifest or None)
        manifest = store.read_manifest()
        sources = manifest.get("sources") or []
        first = (sources[0] if isinstance(sources, list) and sources
                 else {}) or {}
        self.replay_source = {
            "sourceRunId": manifest.get("runId"),
            "sourceRunDir": str(store.run_dir),
            "sourceId": first.get("sourceId"),
            "sourceKind": first.get("kind"),
            "sourceSchemaVersion": first.get("schemaVersion"),
            "sourceDigest": first.get("digest"),
            # The mode of the RECORDING, kept distinct from the mode of the
            # session.  It rides along as provenance and never becomes the
            # session's mode: a recording of a live run, read back, is a Replay.
            "sourceMode": (store.read_summary() or {}).get("sourceMode"),
            "adapter": replay_sources.detect_adapter(path),
        }
        self.controller.record_event(
            lane="SESSION", kind="REPLAY_SOURCE_ATTACHED",
            title=str(manifest.get("runId") or path),
            detail=dict(self.replay_source))
        return store

    # -- mode selection ------------------------------------------------------ #

    def attach_live_integration(self, integration: Any, *,
                                llm_manager: Any = None,
                                coordinator: Any = None,
                                evidence_records: Any = ()) -> Any:
        """Bind this console to one named deployment.  Worker-thread function.

        Binding is not connecting: nothing is contacted here.  It is the
        console admitting that it now knows *which* deployment a Live session
        would be against, which is the precondition Preflight needs before it
        can find - or fail to find - a reachable R1 transport.

        Any recorded source is dropped at the same time.  A console that held a
        recording and a deployment at once could start a session whose evidence
        named one and whose data came from the other.
        """
        from .session.composition import LiveComposition

        manager = llm_manager if llm_manager is not None else self.llm_manager
        if coordinator is None:
            # Part of the runtime that was handed in, not something the console
            # went and found: a port with no coordinator behind it still runs
            # episodes, and the topology row then says no adapter is attached
            # rather than claiming one is.
            coordinator = getattr(self.legacy_episode, "coordinator", None)
        self.replay_source = None
        self.live = LiveComposition(
            integration=integration, llm_manager=manager,
            coordinator=coordinator,
            episode_support=self.legacy_episode,
            evidence_records=tuple(evidence_records or ()))
        self.capability_manifest = dict(integration.capability_manifest)
        # Binding a deployment does not select Live.  The operator still has to
        # ask for it, and Preflight still has to have seen a transport.
        self.requested_mode = None
        self.intent_submitter = self._live_intent_submitter
        from .sources.live import project_deployment_provenance

        self.controller.set_deployment_provenance(
            project_deployment_provenance(integration))
        self.controller.record_event(
            lane="SESSION", kind="LIVE_INTEGRATION_ATTACHED",
            title=str(integration.identity().get("capabilityManifestId")
                      or integration.integration_path),
            detail={"integration": self.live.identity(),
                    "meaning": "the console knows which deployment a Live "
                               "session would address; nothing was contacted "
                               "and no session was started"})
        return self.live

    def attach_live_from_profile(self, *, state_dir: Any = None,
                                 runtime_values: Optional[Mapping[str, Any]] = None,
                                 insecure_dev: bool = False,
                                 endpoints_from_vector: bool = False,
                                 llm_manager: Any = None,
                                 coordinator: Any = None,
                                 evidence_records: Any = ()) -> Any:
        """Bind the deployment the loaded profile names.  Worker-thread function.

        ``runtime_values`` is the deployment's own resolved endpoint set, for a
        deployment that publishes its endpoints through a digest-pinned vector
        rather than in the integration-values document.  Nothing is defaulted:
        a profile that names no integration document is refused here, with the
        reason an operator can act on.
        """
        from .session.composition import NO_LEGACY_EPISODE

        support = self.legacy_episode
        if support is None:
            raise SessionError(
                "this profile names a legacy integration-values document, and "
                + NO_LEGACY_EPISODE)
        path = self.profile.integration_values_path
        if not path:
            raise SessionError(
                f"profile {self.profile.profile_id} names no "
                "integrationValuesPath, so it cannot address a deployment; "
                "Live is unavailable for it")
        root = Path(state_dir) if state_dir else (
            Path(self.profile.runs_root) / "live-state")
        integration = support.load_integration(
            path, state_dir=root, runtime_values=runtime_values,
            insecure_dev=insecure_dev,
            endpoints_from_vector=endpoints_from_vector)
        return self.attach_live_integration(
            integration, llm_manager=llm_manager,
            coordinator=coordinator,
            evidence_records=evidence_records)

    def detach(self, *, reason: str = "operator disconnected") -> None:
        """Return to Disconnected: no deployment, no recording, no claim."""
        self.live = None
        self.replay_source = None
        self.requested_mode = None
        self.intent_submitter = None
        # The proposer inventory belonged to the deployment that was attached.
        # Leaving it on screen would show a Replay session a model list it has
        # no use for and no longer holds.
        self.controller.set_llm_backends((), None)
        self.controller.set_deployment_provenance(None)
        self.controller.record_event(
            lane="SESSION", kind="DISCONNECTED", title=reason,
            detail={"meaning": "no deployment and no recorded source is "
                               "attached; the console observes nothing"})
        try:
            self.controller.mark_disconnected()
        except SessionError as exc:
            # A running session keeps its mode; the badge must not go quiet
            # while a run is still recording.
            self._warn(str(exc))

    def select_mode(self, mode: str) -> str:
        """Record what the operator asked for, and refuse what cannot be given.

        The request is checked against the attachments, not against a preference
        - asking for LIVE with no deployment bound is refused here rather than
        producing a session that claims LIVE with no transport behind it.
        """
        wanted = str(mode or "").strip().upper()
        if wanted not in SELECTABLE_MODES:
            raise SessionError(
                f"{mode!r} is not a mode an operator selects; expected one of "
                f"{', '.join(SELECTABLE_MODES)}")
        if wanted == "LIVE" and not self._live_backing():
            raise SessionError(
                "Live needs a runtime behind it: attach a Kernel submission "
                "session declaring LIVE - the deployed path, wired by the "
                "deployment's composition root - before selecting Live")
        if wanted == "REPLAY" and not self.replay_source:
            raise SessionError(
                "Replay needs a recorded source: load a capture or a stored "
                "run before selecting Replay")
        self.requested_mode = wanted
        self.controller.record_event(
            lane="SESSION", kind="MODE_SELECTED", title=wanted,
            detail={"mode": wanted,
                    "hasIntegration": self.live is not None,
                    "hasKernelSession": self._kernel_live_session() is not None,
                    "hasRecordedSource": bool(self.replay_source)})
        return wanted

    def _kernel_live_session(self) -> Optional[Any]:
        """The attached Kernel session, but only when it declares ``LIVE``.

        A MOCK session is the hardware-free actuation adapter and a REPLAY
        session read a recorded event stream; neither actuated a deployment, so
        neither may back a session that claims Live.  The declaration comes
        from whoever wired the vertical path, because only they know what is
        behind the Write Gateway.
        """
        session = getattr(self, "kernel_session", None)
        if session is None:
            return None
        return session if str(getattr(session, "mode", "")) == "LIVE" else None

    def _live_backing(self) -> Optional[str]:
        """What could back a Live session right now, or ``None``.

        ``KERNEL`` is the deployed answer.  ``LEGACY_EPISODE`` exists only on a
        build that was handed the preserved Coordinator runtime, and it is
        reported separately so a run's own evidence says which one it was
        rather than both reading as "Live".
        """
        if self._kernel_live_session() is not None:
            return "KERNEL"
        if self.live is not None:
            return "LEGACY_EPISODE"
        return None

    def session_mode_evidence(self) -> Tuple[str, dict]:
        """The mode of the next session, and the evidence that backs it.

        Read off what is actually attached; there is no control that sets it.

        * a Kernel submission session that declares ``LIVE`` backs ``LIVE`` -
          the deployed path, where the write left through the Write Gateway;
        * on a build handed the preserved episode runtime, a reachable R1
          transport - ``PF-R1-BOOTSTRAP`` OK in the last Preflight - backs
          ``LIVE`` for that runtime, and the basis says which one it was;
        * an attached recorded source backs ``REPLAY``;
        * nothing attached backs nothing, and this raises.

        Refusing is the point.  ``SessionStore.create`` will reject a REPLAY run
        whose basis is not ``REPLAY_OF_RECORDED_SOURCE``, and inventing that
        basis to get past it would put "replay of a recorded source" in the
        manifest of a session that read no recording at all.  The manifest is
        what the export banner, the badge and the figure watermark are built
        from, so a lie there is a lie in the paper figure.
        """
        checks = {view.check_id: view for view in
                  self.controller.preflight_results}
        bootstrap = checks.get("PF-R1-BOOTSTRAP")
        kernel = self._kernel_live_session()
        live_ok = bootstrap is not None and bootstrap.status == st.OK
        requested = self.requested_mode
        if requested == "REPLAY" and not self.replay_source:
            raise SessionError(
                "Replay was selected but no recorded source is attached")
        if requested == "LIVE" and kernel is not None:
            # The Kernel's own declaration, not a transport probe: what makes
            # this session Live is that the Write Gateway wrote to a live
            # deployment, and only whoever wired that path can say so.
            from .sources.kernel_live import MODE_REASON

            return "LIVE", {
                "basis": "LIVE_KERNEL_WRITE_GATEWAY",
                "claimedBy": "OperatorConsole",
                "reason": MODE_REASON.get(str(getattr(kernel, "mode", "")),
                                          "declared by the composition root"),
                "detail": f"case {getattr(kernel, 'case_id', '?')}",
                "kernelCaseId": getattr(kernel, "case_id", None),
                "kernelSessionMode": str(getattr(kernel, "mode", "")),
            }
        if requested == "LIVE" and not live_ok:
            if bootstrap is None:
                raise SessionError(
                    "Live was selected but Preflight has not run, so no R1 "
                    "transport has been observed")
            raise SessionError(
                "Live was selected but Preflight found no reachable R1 "
                "transport: "
                + str(bootstrap.reason or bootstrap.detail or bootstrap.status))
        if requested == "REPLAY":
            # An operator who asked for Replay gets Replay even when a
            # transport happens to answer.  Silently upgrading the session to
            # LIVE would put live provenance on replayed data.
            live_ok = False
        if live_ok:
            identity = self.live.identity() if self.live is not None else {}
            return "LIVE", {
                "basis": "LIVE_R1_TRANSPORT",
                "claimedBy": "OperatorConsole",
                "reason": "R1 service discovery answered during Preflight",
                "detail": bootstrap.detail,
                **{f"integration{key[:1].upper()}{key[1:]}": value
                   for key, value in identity.items()
                   if key in ("capabilityManifestId", "nearRtRicId",
                              "r1ApiRoot", "contractProfile")},
            }
        if self.replay_source:
            source = dict(self.replay_source)
            return "REPLAY", {
                "basis": "REPLAY_OF_RECORDED_SOURCE",
                "claimedBy": "OperatorConsole",
                "reason": "the console is reading a recorded source; nothing "
                          "is being observed now",
                **source,
            }
        raise SessionError(
            "no session source is attached: Preflight found no reachable R1 "
            "transport, and no recorded source has been loaded. Load a Replay "
            "source, or attach an R1 client, before starting a session.")

    def _action_start(self, _payload: str) -> None:
        try:
            mode, evidence = self.session_mode_evidence()
        except SessionError as exc:
            self._warn(str(exc))
            return
        self.controller.run_in_worker(
            "session-start",
            lambda: self.controller.start(mode=mode, mode_evidence=evidence))

    def _action_stop(self, _payload: str) -> None:
        self.controller.run_in_worker("session-stop",
                                      lambda: self.controller.stop("COMPLETED"))

    def _action_abort(self, _payload: str) -> None:
        spec = self.controller.abort_confirmation()
        if not self._confirmed(spec):
            self._warn("abort cancelled", kind="ACTION_CANCELLED")
            return
        self.controller.run_in_worker("session-abort", self.controller.abort)

    def _action_submit(self, payload: str) -> None:
        text = (payload or "").strip()
        if not text:
            self._warn("no intent text was entered")
            return
        if self.intent_submitter is None:
            self._warn("no intent submitter is registered in this build; the "
                       "Intent & Decision workspace is not present")
            return
        # Three refusals that all protect the same property: one operator
        # action must produce at most one coordinator episode, recorded into a
        # run that is open.
        if self.controller.disposition != "RUNNING":
            self._warn("no session is recording; start a session before "
                       "submitting an intent")
            return
        if self.controller.stop_requested:
            self._warn("this session is finalizing; the intent was not "
                       "submitted")
            return
        in_flight = getattr(self.live, "in_flight", None)
        if in_flight:
            self._warn(f"intent {in_flight} is still running; wait for it to "
                       "settle before submitting another")
            return
        spec = spec_for(
            "C-INTENT-SUBMIT", title="Submit intent",
            targets=(f"run {self.controller.run_id or 'none'}",
                     f"profile {self.profile.profile_id}",
                     f"intent text: {text[:120]}"),
            effects=("the coordinator runs one S0-S6 episode",
                     "an A1 policy may be created through the Non-RT RIC "
                     "Framework over R1"))
        if not self._confirmed(spec):
            self._warn("intent submission cancelled", kind="ACTION_CANCELLED")
            return
        submitter = self.intent_submitter
        self.controller.record_event(lane="INTENT", kind="INTENT_SUBMITTED",
                                     title=text)
        self.controller.run_in_worker("intent-submit",
                                      lambda: submitter(text))
        self.footer.clear_intent()

    def _action_preview(self, payload: str) -> None:
        """Show what would actually be submitted, and whether it can be.

        The console does **not** parse the sentence: normalization is an LLM
        step and it happens inside the episode, once, where it is recorded.
        Previewing it here would either be a second parse - a second model call
        whose answer the episode need not agree with - or a guess.

        What the preview *can* answer honestly is the question an operator
        actually has before pressing Submit: which deployment this goes to,
        under which policy scope and validity, and whether the profile carries
        every value the contract forbids defaulting.  A profile that is missing
        one is refused here by name, before an episode exists.  When a previous
        episode has run, its own normalized intent is shown beside that, marked
        as coming from that episode.
        """
        text = (payload or "").strip()
        if not text:
            self._warn("no intent text was entered")
            return

        def _run() -> None:
            composition = self.live
            preview: dict = {
                "intentText": text,
                "normalizationNote": "the intent is normalized by the "
                                     "coordinator inside the episode; the "
                                     "console does not parse it a second time",
            }
            severity = "INFO"
            reason = None
            decision = self.controller.state().decision
            if decision is not None and decision.normalized_intent:
                preview["lastEpisodeNormalizedIntent"] = dict(
                    decision.normalized_intent)
                preview["lastEpisodeId"] = decision.episode_id
            if composition is None:
                severity, reason = "WARNING", (
                    "no deployment is attached; this intent cannot be "
                    "submitted from here")
            else:
                context = {**dict(self.profile.policy_context or {}),
                           "intentId": "00000000-0000-0000-0000-000000000000"}
                try:
                    request = composition.integration.episode_request(
                        intent_text=text, policy_context=context)
                    preview.update({
                        "integration": composition.identity(),
                        "objectiveKind": context.get("objectiveKind"),
                        "allowedCells": len(context.get("allowedCells") or ()),
                        "validity": {"notBefore": context.get("notBefore"),
                                     "expiresAt": context.get("expiresAt")},
                        "policyRevision": context.get("policyRevision"),
                        "statePath": request.get("statePath"),
                        "submittable": True,
                        "intentIdNote": "a fresh contract intent id is minted "
                                        "at submit; the zero uuid above is a "
                                        "placeholder for this check only",
                    })
                except Exception as exc:
                    severity = "WARNING"
                    preview["submittable"] = False
                    reason = f"{type(exc).__name__}: {exc}"
            self.controller.record_event(
                lane="INTENT", kind="INTENT_PREVIEWED", severity=severity,
                title=text[:120], detail={**preview, "reason": reason})

        self.controller.run_in_worker("intent-preview", _run)

    def _action_withdraw(self, _payload: str) -> None:
        """Withdrawal is stated as unsupported rather than faked.

        ``gui.operator.sources.live.withdraw_intent`` exists and takes an
        injected *real* lifecycle operation, precisely so that no local row is
        ever cleared optimistically.  The upper contract exposes no such
        operation, so nothing is injected and the action refuses with the
        contract reason - which is the honest state of GAP-04, and what the
        button must say until a deployment offers the operation.
        """
        from .session.composition import WITHDRAWAL_UNSUPPORTED

        self._warn(WITHDRAWAL_UNSUPPORTED, kind="ACTION_UNSUPPORTED")

    def _action_llm_refresh(self, _payload: str) -> None:
        self.controller.run_in_worker(
            "llm-refresh", lambda: self.refresh_backends(discover=True))

    # -- Agent sitting: operator model choices and execution -----------------

    def _role_models_path(self):
        from pathlib import Path as _Path
        from assurance.coordination import DEFAULT_ROLE_MODELS_FILENAME
        return _Path(self.profile.runs_root) / DEFAULT_ROLE_MODELS_FILENAME

    def _load_role_models(self) -> dict:
        try:
            from assurance.coordination import load_role_models_file
            models = load_role_models_file(self._role_models_path())
            result = {**models.to_record(), "method": models.method}
            import json
            record = json.loads(self._role_models_path().read_text())
            if "settings" in record:
                result["settings"] = record["settings"]
            return result
        except Exception:
            return {"target": None, "control": None, "trajectory": None, "monolith": None,
                    "method": "three-agent"}

    def _workspace_role_models(self, chosen) -> None:
        """Record which LLM carries each role for the next Agent sitting.

        This is an operator choice, kept beside the runs; it changes which
        model proposes targets, controls or the next trial and nothing the
        Kernel decides.  ``main.py --agent`` reads the same file.
        """
        from datetime import datetime, timezone
        from assurance.coordination import RoleModels, save_role_models_file
        try:
            models = RoleModels.from_mapping({key: value for key, value in chosen.items() if key != "settings"})
            path = save_role_models_file(
                self._role_models_path(), models,
                chosen_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
            import json
            record = json.loads(path.read_text())
            record["schemaVersion"] = "agent-role-models/2.1.0"
            if "settings" in chosen:
                record["settings"] = chosen["settings"]
            path.write_text(json.dumps(record, indent=2) + "\n")
        except Exception as exc:
            self._warn(f"the role-model choice was not recorded: {exc}")
            return
        summary = ", ".join(f"{r}={m or 'deterministic'}" for r, m in models.to_record().items())
        self.controller.record_event(
            lane="ADVISORY", kind="AGENT_ROLE_MODELS", severity="INFO",
            title=f"Agent sitting {models.method}: {summary} (recorded at {path})")

    @staticmethod
    def _sitting_request(payload):
        """Map operator inputs at the composition boundary."""
        from tools.liveconsole.agent import AgentRequest
        records = tuple(payload.get("intents", ()))
        settings = dict(payload.get("settings", {}))
        exposure = settings.pop("axisExposure", {})
        recorded_sentences = {row.get("sentence") for row in records if row.get("sentence")}
        return AgentRequest(
            sentences=tuple(text for text in payload.get("sentences", ()) if text not in recorded_sentences),
            intents=tuple(payload.get("intents", ())),
            method=payload.get("method", "three-agent"),
            role_models=dict(payload.get("roleModels", {})),
            budget_trials=int(payload.get("budgetTrials", 16)),
            # ``timingMode`` is a sitting setting, not an intent field: it says
            # whether B measures from the live trigger or from input release.
            timing_mode=str(settings.pop("timingMode", "prepared") or "prepared"),
            settings=settings, **exposure,
            answers=dict(payload.get("answers", {})),
            clarification_round=int(payload.get("clarificationRound", 0)))

    def _workspace_run_sitting(self, payload):
        from threading import Event
        if getattr(self, "_sitting_busy", False):
            return
        session = self.kernel_session
        mode = getattr(session, "mode", None)
        if mode not in ("MOCK", "LIVE"):
            self.bus.publish("decision", {"kind": "agent-sitting", "running": False,
                                         "status": "Attach a MOCK or LIVE session before running"})
            return
        self._sitting_busy = True
        self._sitting_stop = Event()
        self._active_sitting = None
        self.bus.publish("decision", {"kind": "agent-sitting", "running": True,
                                     "status": f"Preparing {mode} sitting"})

        def publish(status, running=True):
            sitting = self._active_sitting
            self.bus.publish("decision", {
                "kind": "agent-sitting", "status": status, "running": running,
                "episode": {**sitting.episode().to_record(),
                    "execution": {"preflight": dict(getattr(sitting, "preflight", {}))},
                    "catalogCardinality": sitting.preview().get("catalogCardinality")
                    if hasattr(sitting, "preview") else None} if sitting is not None else {},
                "functionCatalog": sitting.preview().get("functionCatalog", [])
                if sitting is not None and hasattr(sitting, "preview") else []})

        def run():
            from tempfile import mkdtemp
            from tools.liveconsole.agent import (
                build_agent_sitting, build_hardware_free_agent_sitting, write_agent_evidence,
                ClarificationNeeded)
            try:
                request = self._sitting_request(payload)
                self._workspace_role_models({**dict(request.role_models), "method": request.method,
                                             "settings": {**dict(request.settings),
                                                 **({"axisExposure": payload["settings"]["axisExposure"]}
                                                    if "axisExposure" in payload.get("settings", {}) else {})}})
                if mode == "MOCK":
                    root = Path(self.profile.runs_root) / "agent-hardware-free"
                    root.mkdir(parents=True, exist_ok=True)
                    sitting = build_hardware_free_agent_sitting(
                        request, tmp_dir=mkdtemp(prefix="sitting-", dir=root))
                else:
                    sitting = build_agent_sitting(self.profile.source_path, request)
                self._active_sitting = sitting
                publish(f"{mode}: T and C prepared")
                if self._sitting_stop.is_set():
                    sitting.request_stop()
                sitting.confirm()
                sitting.run(
                    on_decision=lambda item: publish(
                        f"{mode}: {item.get('role', 'agent')} decision"),
                    on_trial=lambda item: publish(f"{mode}: trial {item.get('trialIndex')} settled"))
                written = write_agent_evidence(sitting)
                publish(f"{mode}: {sitting.termination} · {written['episode']}", False)
            except ClarificationNeeded as asked:
                self.bus.publish("decision", {
                    "kind": "agent-sitting", "running": False, "status": str(asked),
                    "questions": asked.questions, "refused": asked.refused,
                    "clarificationRound": asked.round, "requestPayload": payload})
            except Exception as exc:
                publish(f"{mode}: sitting failed: {exc}", False)
            finally:
                self._sitting_busy = False
                self._active_sitting = None
        return self.controller.run_in_worker("agent-sitting", run)

    def _workspace_stop_sitting(self):
        stop = getattr(self, "_sitting_stop", None)
        if stop is not None:
            stop.set()
        sitting = getattr(self, "_active_sitting", None)
        if sitting is not None:
            sitting.request_stop()

    def _action_llm_select(self, payload: str) -> None:
        name = (payload or "").strip()
        if not name:
            self._warn("no LLM backend was selected")
            return
        composition = self.live
        if composition is None and self.llm_manager is None:
            self._warn("no proposer inventory is attached; there is nothing "
                       "here to select from")
            return
        if composition is None:
            # The advisory inventory with no episode composition behind it -
            # the deployed build.  Selecting still records what the operator
            # chose, and it still changes nothing the Kernel decides.
            def _run_advisory() -> None:
                from .sources.llm_registry import LlmRegistry

                registry = LlmRegistry(self.llm_manager)
                try:
                    applied = bool(registry.select(name))
                    message = (f"{name} is the advisory proposer" if applied
                               else f"{name} was refused by the inventory")
                except Exception as exc:
                    applied, message = False, (
                        f"{name} was not selected: {type(exc).__name__}: {exc}")
                self.controller.record_event(
                    lane="DECISION", kind="LLM_BACKEND_SELECTED",
                    severity="INFO" if applied else "WARNING", title=message,
                    detail={"requested": name, "applied": applied,
                            "advisoryOnly": True,
                            "meaning": "the proposer inventory is advisory on "
                                       "this runtime; no bound, verdict or "
                                       "closure depends on it"})
                self.refresh_backends()

            self.controller.run_in_worker("llm-select", _run_advisory)
            return

        def _run() -> None:
            applied, message = composition.select_backend(name)
            self.controller.record_event(
                lane="DECISION", kind="LLM_BACKEND_SELECTED",
                severity="INFO" if applied else "WARNING",
                title=message,
                detail={"requested": name, "applied": applied,
                        "activeNow": composition.active_backend(),
                        "appliesFrom": "next episode"})
            self.refresh_backends()

        self.controller.run_in_worker("llm-select", _run)

    def _action_bind_live(self, _payload: str) -> None:
        """Bind the deployment the loaded profile names.  Contacts nothing.

        This is the control the console was missing: every piece of the Live
        path existed and none of it could be reached from the window, so a
        profile that named a deployment could be loaded and then not used.

        Binding stays offline on purpose - it reads the document, its pinned
        capability manifest and (when the deployment publishes them there) its
        pinned endpoints, and stops.  The first connection an operator makes is
        Preflight, and it is still Preflight that decides whether Live is
        available at all.
        """
        if not self.profile.integration_values_path:
            self._warn(
                f"profile {self.profile.profile_id} names no "
                "integrationValuesPath; load a profile that names the "
                "deployment to bind", kind="ACTION_UNSUPPORTED")
            return

        def _run() -> None:
            approved = os.environ.get(LOOPBACK_DEPLOYMENT_ENV,
                                      "").strip() == "1"
            try:
                self.attach_live_from_profile(
                    endpoints_from_vector=approved, insecure_dev=approved)
            except SessionError as exc:
                # A stated refusal on the timeline, not a worker traceback.
                # On the deployed build this is the expected answer: the
                # profile names a legacy deployment and this console has no
                # runtime that could address one.
                self._warn(str(exc), kind="ACTION_UNSUPPORTED")
                return
            self.refresh_status()
            self.refresh_backends()

        self.controller.run_in_worker("bind-live", _run)

    def _action_load_replay(self, payload: str) -> None:
        """Attach a recorded capture or run as the session's source."""
        path = (payload or "").strip() or self._ask_open_directory()
        if not path:
            return

        def _run() -> None:
            self.attach_replay_source(path)

        self.controller.run_in_worker("load-replay", _run)

    def _action_mode_live(self, _payload: str) -> None:
        self.select_mode("LIVE")

    def _action_mode_replay(self, _payload: str) -> None:
        self.select_mode("REPLAY")

    def _action_disconnect(self, _payload: str) -> None:
        if self.controller.disposition == "RUNNING":
            self._warn("stop the running session before disconnecting")
            return
        self.detach()

    def _action_load_run(self, payload: str) -> None:
        path = (payload or "").strip() or self._ask_open_directory()
        if not path:
            return

        def _run() -> None:
            from .store.session_store import SessionStore

            store = SessionStore.open(path)
            self.show_run(store)
            self.controller.record_event(
                lane="SESSION", kind="RUN_LOADED", title=str(path),
                detail={"runId": getattr(store, "run_id", None),
                        "mode": getattr(store, "mode", None)})

        self.controller.run_in_worker("load-run", _run)

    def _action_export(self, payload: str) -> None:
        figure_id = (payload or "").strip() or "operator-export"
        self.controller.run_in_worker(
            "export", lambda: self.export_current_run(figure_id=figure_id))

    def export_current_run(self, *, figure_id: str = "operator-export",
                           destination: Any = None) -> Mapping[str, Any]:
        """Export the current or most recently loaded run.  Worker thread.

        The export is the run's own data plus the identity needed to read it
        later: mode, source, times, the contract and release identifiers of the
        deployment it addressed, and the model provenance of the decision.  An
        export that could not say which deployment produced it would be a
        dataset nobody can reproduce.
        """
        from .export.data_export import export_run
        from .store.session_store import SessionStore

        run_dir = self._exportable_run_dir()
        if not run_dir:
            raise SessionError("no run is available to export")
        store = SessionStore.open(run_dir)
        target = Path(destination) if destination else Path(run_dir) / "export"
        manifest = dict(export_run(store, target))
        context = {
            "figureId": figure_id,
            "exportedAt": utc_now(),
            "mode": manifest.get("mode"),
            "integration": (self.live.identity() if self.live is not None
                            else {}),
            "replaySource": dict(self.replay_source or {}),
            "profileId": self.profile.profile_id,
            "activeBackend": (self.live.active_backend()
                              if self.live is not None else None),
        }
        try:
            (target / "OPERATOR-CONTEXT.json").write_text(
                json.dumps(context, indent=2, sort_keys=True, default=str)
                + "\n", encoding="utf-8")
        except OSError:
            logger.debug("operator context not written", exc_info=True)
        self.controller.record_event(
            lane="SESSION", kind="EXPORT_WRITTEN", title=str(target),
            detail={"files": sorted(manifest.get("files") or ()),
                    "mode": manifest.get("mode"),
                    "context": context})
        return {**manifest, "operatorContext": context,
                "exportDir": str(target)}

    def _exportable_run_dir(self) -> Optional[str]:
        store = getattr(self.controller, "_store", None)
        run_dir = getattr(store, "run_dir", None)
        if run_dir:
            return str(run_dir)
        workspace = self.workspace("analysis")
        model = getattr(workspace, "model", None)
        primary = getattr(model, "primary", None)
        view = primary() if callable(primary) else primary
        return getattr(view, "run_dir", None)

    def _action_profile_new(self, _payload: str) -> None:
        self.set_profile(default_profile())

    def _action_profile_load(self, payload: str) -> None:
        path = payload.strip() or self._ask_open_path()
        if not path:
            return
        try:
            self.set_profile(ExperimentProfile.load(path))
        except ProfileError as exc:
            self._warn(f"profile not loaded: {exc}")

    def _action_profile_save(self, payload: str) -> None:
        path = payload.strip() or self._ask_save_path()
        if not path:
            return
        written = self.profile.save(path)
        self.controller.record_event(lane="SESSION", kind="PROFILE_SAVED",
                                     title=str(written))

    # -- the Kernel submission path (Gate 3) ---------------------------------- #
    #
    # The five controls of design section 5, minus Confirm Batch Plan, routed
    # onto one KernelSubmissionSession.  Each of them either refuses with a
    # stated reason on the timeline or hands the work to a worker; none of them
    # touches the Kernel from the thread that repaints, and none of them
    # carries a value the operator typed into anything the Kernel decides.

    def attach_kernel_session(self, session: Any) -> Any:
        """Adopt the Kernel submission session this console drives.

        Composition hands the console an already-wired session: which
        deployment is behind the Write Gateway, and whether it is live or the
        hardware-free mock adapter, is not something a console may choose.

        Adopting it also redirects where it emits, so the Cockpit header,
        Trial & Safety and the Evidence Ledger see every poll of the Kernel's
        own loop rather than only the end of it.
        """
        self.kernel_session = session
        if session is not None:
            setter = getattr(session, "set_publisher", None)
            if setter is not None:
                # Chained, never replaced: whoever built the session may
                # already have wired a receiver, and adopting it must not
                # unhook them.  The console adds the Cockpit projection to
                # what the session already emits.
                existing = getattr(session, "publisher", None)
                setter(self._kernel_publish_after(existing))
            self.controller.record_event(
                lane="SESSION", kind="KERNEL_SESSION_ATTACHED",
                title=f"case {getattr(session, 'case_id', '?')} "
                      f"({getattr(session, 'mode', '?')})")
            self.refresh_cockpit()
        return session

    # -- the Cockpit projection --------------------------------------------- #

    def _kernel_publish_after(self, existing: Optional[Callable[[str, Any],
                                                                 None]]
                              ) -> Callable[[str, Any], None]:
        """Wrap ``existing`` so the Cockpit projection follows every emit.

        Two publishes, not one: the Contract Studio reads the session view on
        ``decision``, and the three Kernel-backed Cockpit surfaces read the
        projection on ``cockpit``.  Projecting here rather than on the render
        thread is what keeps the reduced-state replay off the Tk thread.
        """

        def publish(channel: str, payload: Any) -> None:
            if existing is not None:
                existing(channel, payload)
            else:
                self.bus.publish(channel, payload)
            self.refresh_cockpit()

        return publish

    def refresh_cockpit(self) -> Optional[Any]:
        """Re-project the Kernel and publish it.  **Worker thread.**

        Returns the snapshot, or ``None`` when there is no Kernel session --
        in which case the header keeps saying so rather than keeping a stale
        reading from a session that is gone.
        """
        session = getattr(self, "kernel_session", None)
        if session is None:
            self.cockpit = None
            return None
        try:
            snapshot = cockpit_source.project_session(session)
        except Exception:                                  # pragma: no cover
            logger.exception("cockpit projection failed")
            return None
        self.cockpit = snapshot
        self.bus.publish("cockpit", snapshot)
        return snapshot

    def cockpit_view(self) -> Any:
        """The header projection to paint, never a stale or invented one."""
        snapshot = self.cockpit
        if snapshot is not None:
            return snapshot.header
        return cockpit_source.CockpitHeaderView(
            mode=self._state.mode,
            unavailable_reason="no Kernel session is attached to this console")

    # -- Batch Experiments ---------------------------------------------------- #

    def attach_batch_sessions(self, factory: Optional[Callable[[Any], Any]],
                              *, mode: Optional[str] = None) -> None:
        """Declare how a batch case gets a Kernel session.

        A composition fact, exactly like :meth:`attach_kernel_session`: only
        whoever wired the vertical path knows what is behind the gateway, so
        the console is told rather than deciding.  Passing ``None`` removes the
        capability, and Start Batch then refuses by name.
        """
        self.batch_session_factory = factory
        session = self._batch_session(create=factory is not None)
        if session is not None:
            session.bind_sessions(factory, mode=mode)

    def _batch_session(self, *, create: bool = True) -> Optional[Any]:
        if (self.batch is not None and not self.batch.scope_locked
                and str(self.batch.runs_root) != str(self.profile.runs_root)):
            # A profile loaded after the pane was first opened moves the runs
            # root.  A batch session holding the old one would write its run
            # directory somewhere the operator is no longer looking; a running
            # plan keeps the root it was confirmed against.
            self.batch.runs_root = Path(self.profile.runs_root)
        if self.batch is None and create:
            session = getattr(self, "kernel_session", None)
            self.batch = batch_source.BatchSession(
                runs_root=self.profile.runs_root,
                session_factory=self.batch_session_factory,
                mode=str(getattr(session, "mode", "MOCK")),
                publish=self.bus.publish)
            self.bus.publish("batch", self.batch.view())
        return self.batch

    def _batch_worker(self, name: str, call: Callable[[], Any]) -> None:
        def _run() -> None:
            try:
                call()
            except batch_source.BatchRefused as exc:
                self.controller.record_event(
                    lane="WARNING", kind="BATCH_REFUSED", severity="WARNING",
                    title=f"{exc.code}: {exc.detail}" if exc.detail
                          else exc.code)
            except Exception as exc:
                self.controller.record_event(
                    lane="WARNING", kind="BATCH_FAILED", severity="ERROR",
                    title=f"{type(exc).__name__}: {exc}")
            finally:
                session = self.batch
                if session is not None:
                    self.bus.publish("batch", session.view())

        self.controller.run_in_worker(name, _run)

    def _action_batch_edit(self, payload: str) -> None:
        """``key=value`` from the plan form.  One field, no coercion."""
        key, _, value = (payload or "").partition("=")
        key = key.strip()
        if not key:
            self._warn("no batch plan field was named")
            return
        session = self._batch_session()
        try:
            session.edit(key, value.strip())
        except batch_source.BatchRefused as exc:
            self._warn(f"{exc.code}: {exc.detail}" if exc.detail else exc.code,
                       kind="ACTION_REFUSED")

    def _action_batch_confirm(self, _payload: str) -> None:
        session = self._batch_session()
        view = session.view()
        if not view.plan_content_hash:
            self._warn(view.refusal or "the batch draft is not an admissible "
                                       "plan", kind="ACTION_REFUSED")
            return
        spec = spec_for(
            "C-BATCH-PLAN", title="Confirm Batch Plan",
            targets=(f"{view.case_count} bounded case(s)",
                     f"objectives {session.draft.objectives}",
                     f"strategy {session.draft.strategy}"),
            effects=(f"content hash {view.plan_content_hash}",
                     "the plan scope is fixed for the whole repetition",
                     "editing any field invalidates this confirmation"))
        if not self._confirmed(spec):
            self._warn("batch plan confirmation cancelled",
                       kind="ACTION_CANCELLED")
            return
        self._batch_worker("batch-confirm", session.confirm)

    def _action_batch_start(self, _payload: str) -> None:
        session = self._batch_session()
        view = session.view()
        if not view.confirmation_valid:
            self._warn("confirm the bounded plan before starting it",
                       kind="ACTION_REFUSED")
            return
        self.controller.record_event(
            lane="SESSION", kind="BATCH_STARTED",
            title=f"{view.case_count} case(s), plan {view.plan_content_hash}")
        self._batch_worker("batch-start", session.start)

    def _kernel_session(self) -> Optional[Any]:
        session = getattr(self, "kernel_session", None)
        if session is None:
            self._warn("no Kernel session is attached; the Contract Studio "
                       "has nothing to submit to", kind="ACTION_UNSUPPORTED")
        return session

    def _kernel_worker(self, name: str, call: Callable[[], Any]) -> None:
        """Run one Kernel control on a worker, reporting its refusal by name."""
        session = self._kernel_session()
        if session is None:
            return

        def _run() -> None:
            try:
                call()
            except Exception as exc:
                reason = getattr(exc, "reason", type(exc).__name__)
                detail = getattr(exc, "detail", "") or str(exc)
                self.controller.record_event(
                    lane="WARNING", kind="KERNEL_REFUSED", severity="WARNING",
                    title=f"{reason}: {detail}" if detail else str(reason))
                self.bus.publish("decision", session.view())

        self.controller.run_in_worker(name, _run)

    def _action_kernel_draft(self, payload: str) -> None:
        text = (payload or "").strip()
        if not text:
            self._warn("no intent text was entered")
            return
        session = self._kernel_session()
        if session is None:
            return
        self.controller.record_event(lane="INTENT", kind="KERNEL_INTENT_DRAFT",
                                     title=text)
        self._kernel_worker("kernel-draft", lambda: session.draft(text))

    def _action_kernel_confirm(self, _payload: str) -> None:
        session = self._kernel_session()
        if session is None:
            return
        preview = session.preview
        if preview is None:
            self._warn("draft a contract before confirming it")
            return
        # The confirmation the Kernel records is over the content hash; this
        # spec is what the operator is shown before that record is written, and
        # it names the same hash so the two cannot describe different things.
        # The preconditions are shown, not merely hashed.  They are the things
        # this confirmation authorises the deployment to *do* before the
        # contract can be applied -- withdrawing a policy that recorded a real
        # effect, for one -- and an operator cannot cover an act they were not
        # shown.  They are in the content hash too, so a changed one voids this.
        preconditions = tuple(getattr(preview, "preconditions", ()) or ())
        spec = spec_for(
            "C-INTENT-SUBMIT", title="Review & Confirm contract",
            targets=(f"case {session.case_id}",
                     f"candidate {preview.candidate_id}",
                     f"parameters {dict(preview.parameters)}"),
            effects=tuple(f"precondition: {item}" for item in preconditions)
            + (f"content hash {preview.content_hash()}",
               "the draft becomes a NORMATIVE contract instance",
               "changing the content invalidates this confirmation"))
        if not self._confirmed(spec):
            self._warn("confirmation cancelled", kind="ACTION_CANCELLED")
            return
        self._kernel_worker("kernel-confirm", session.confirm)

    def _action_kernel_start(self, _payload: str) -> None:
        session = self._kernel_session()
        if session is None:
            return
        preview = getattr(session, "preview", None)
        preconditions = tuple(getattr(preview, "preconditions", ()) or ())
        spec = spec_for(
            "C-INTENT-SUBMIT", title="Confirm and Start",
            targets=(f"case {session.case_id}",
                     f"mode {session.mode}"),
            effects=tuple(f"precondition: {item}" for item in preconditions)
            + ("the Kernel opens a trial and the Write Gateway applies "
               "the confirmed candidate",
               "the console polls the Kernel until a terminal state"))
        if not self._confirmed(spec):
            self._warn("start cancelled", kind="ACTION_CANCELLED")
            return
        self._kernel_worker("kernel-start", session.start)

    def _action_kernel_abort(self, _payload: str) -> None:
        session = self._kernel_session()
        if session is None:
            return
        spec = spec_for(
            "C-SESSION-ABORT", title="Abort case",
            targets=(f"case {session.case_id}",),
            effects=("the Kernel terminates the case at a safe boundary",
                     "an applied change is not reversed by this control"))
        if not self._confirmed(spec):
            self._warn("abort cancelled", kind="ACTION_CANCELLED")
            return
        self._kernel_worker("kernel-abort", session.abort)

    def _action_kernel_estop(self, _payload: str) -> None:
        """Emergency Stop.  Raised on this thread, acted on off it.

        The raise is deliberately not deferred to a worker: it is a
        non-blocking flag set, and the situation this control exists for is
        precisely the one where a worker is already busy running the trial.
        Once raised, the session's own poll loop honours it at its next Kernel
        boundary; the worker started here only covers the case where no loop is
        running to honour it.
        """
        session = self._kernel_session()
        if session is None:
            return
        self.controller.record_event(
            lane="WARNING", kind="OPERATOR_EMERGENCY_STOP", severity="ERROR",
            title=f"emergency stop raised on case {session.case_id}")
        session.request_emergency_stop()
        self._kernel_worker("kernel-estop", session.emergency_stop)

    # -- the live write path -------------------------------------------------- #

    def _live_intent_submitter(self, text: str) -> Any:
        """One operator submission -> one authoritative episode.  Worker thread.

        Everything after the episode is recording, not deciding: the result is
        projected with the same functions Replay uses, written into the open run
        with its provenance, and published.  If any of that fails the episode
        still happened and is still recorded as an error - the console never
        rewrites an outcome because it could not draw it.
        """
        composition = self.live
        if composition is None:
            raise SessionError(
                "no deployment is attached; an intent cannot be submitted")
        policy_context = dict(self.profile.policy_context or {})
        episode = composition.submit(
            text, policy_context=policy_context,
            run_id=self.controller.run_id,
            present=self._present_episode)
        self._record_live_episode(episode)
        return episode

    def _present_episode(self, snapshot: Mapping[str, Any]) -> None:
        """Presentation only.  Never allowed to change what was decided."""
        try:
            self.bus.publish("decision", None if not snapshot else snapshot)
        except Exception:                                  # pragma: no cover
            logger.debug("episode presentation failed", exc_info=True)

    def _record_live_episode(self, episode: Any) -> None:
        """Write one episode into the open run and publish it."""
        from .sources.live import project_correlation_trace

        store = getattr(self.controller, "_store", None)
        outbound = episode.r1_outbound()
        status = episode.policy_status()
        records = episode.store_records()
        if store is not None:
            for name, payload in (("append_episode", records["episode"]),):
                try:
                    getattr(store, name)(payload)
                except Exception as exc:
                    self._warn(f"episode not recorded: {type(exc).__name__}: "
                               f"{exc}", kind="RECORDING_FAILED")
            for cycle in records["cycles"]:
                try:
                    store.append_cycle(cycle)
                except Exception:
                    logger.debug("cycle not recorded", exc_info=True)
            for call in records["llmCalls"]:
                try:
                    store.append_llm_call(call)
                except Exception:
                    logger.debug("llm call not recorded", exc_info=True)
        correlation_id = outbound.get("correlationId")
        self.controller.record_event(
            lane="R1", kind="R1_POLICY_OUTBOUND",
            severity="INFO" if outbound.get("contractValid") else "WARNING",
            title=str(outbound.get("policyId")
                      or "no policy object was dispatched"),
            intent_id=episode.intent_id,
            policy_id=outbound.get("policyId"),
            episode_id=records["episode"].get("episodeId"),
            correlation_id=correlation_id,
            detail=outbound)
        if status:
            self.controller.record_event(
                lane="A1", kind="A1_POLICY_STATUS_OBSERVED",
                title=str(status.get("enforceStatus") or "no enforceStatus"),
                policy_id=outbound.get("policyId"),
                correlation_id=correlation_id,
                detail={"enforceStatus": status.get("enforceStatus"),
                        "aicStatus": dict(status.get("aicStatus") or {}),
                        "observedAt": utc_now(),
                        "source": "R1_POLICY_STATUS"})
        aic_status = status.get("aicStatus") if isinstance(status, Mapping) else None
        aic_status = aic_status if isinstance(aic_status, Mapping) else {}
        control = aic_status.get("control")
        control = control if isinstance(control, Mapping) else {}
        rollback = aic_status.get("rollback")
        rollback = rollback if isinstance(rollback, Mapping) else {}
        if control:
            self.controller.record_event(
                lane="A1", kind="E2_CONTROL_OBSERVED",
                title=str(control.get("result") or "no control result"),
                policy_id=outbound.get("policyId"),
                correlation_id=correlation_id,
                detail={"result": control.get("result"),
                        "writeMayHaveOccurred": control.get("writeMayHaveOccurred"),
                        "rollbackState": rollback.get("state"),
                        "observedAt": utc_now(),
                        "source": "A1_POLICY_STATUS.aicStatus.control"})
        decision = episode.decision
        self.controller.set_decision(decision)
        self.controller.set_correlation_trace(project_correlation_trace(
            authoritative=episode.authoritative, decision=decision,
            intent_row=episode.intent_row, intent_id=episode.intent_id,
            r1_outbound=outbound, policy_status=status,
            policy_context=episode.policy_context))
        rows = tuple(row for row in self.controller.state().intents
                     if row.intent_id != episode.intent_row.intent_id)
        self.controller.set_intents(rows + (episode.intent_row,))
        self.controller.record_event(
            lane="DECISION", kind="DECISION_OBSERVED", origin="OBSERVED",
            title=f"episode {decision.episode_id} -> "
                  f"{decision.eq12_state or 'unknown'}",
            episode_id=decision.episode_id, intent_id=episode.intent_id,
            correlation_id=correlation_id,
            detail={"terminalOutcome": decision.terminal_outcome,
                    "terminalReason": decision.terminal_reason,
                    "eq12State": decision.eq12_state,
                    "rolledBack": decision.rolled_back,
                    # The S4 verdict beside the terminal state, so a failsafe
                    # can be told apart from a refusal at a glance: an
                    # unresolvable assurance and a violated one are different
                    # facts about the deployment, not about the console.
                    "assuranceDecision": _assurance_of(episode.authoritative),
                    "enforceStatus": (status or {}).get("enforceStatus"),
                    "proposerAtSubmit": episode.proposer_at_submit,
                    "contractIntentId": episode.intent_id,
                    "integration": dict(episode.identity)})
        self.refresh_status()

    def set_profile(self, profile: ExperimentProfile) -> None:
        self.profile = profile
        self.controller.set_profile(profile)
        self._load_profile_capability(profile)

    def _load_profile_capability(self, profile: ExperimentProfile) -> None:
        """Adopt the capability manifest the profile names, if it names one.

        Wired at integration.  ``ExperimentProfile`` has carried
        ``capabilityManifestPath`` since the design step, and the console read
        its manifest only from its constructor - so an operator who started the
        console from ``main.py`` and loaded a profile got a topology grid that
        could never show the deployment's inventory, no matter what the profile
        said.

        A manifest that cannot be read is a warning on the timeline, not an
        exception and not a silent fallback: the grid then shows Unsupported
        with the reason, which is the true statement.
        """
        path = getattr(profile, "capability_manifest_path", None)
        if not path:
            return
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._warn(f"capability manifest not loaded from {path}: {exc}",
                       kind="CAPABILITY_UNREADABLE")
            return
        if not isinstance(document, Mapping):
            self._warn(f"capability manifest at {path} is not a JSON object",
                       kind="CAPABILITY_UNREADABLE")
            return
        self.capability_manifest = dict(document)
        self.controller.record_event(
            lane="SESSION", kind="CAPABILITY_MANIFEST_LOADED",
            title=str(document.get("manifestId") or path),
            detail={"path": str(path),
                    "nearRtRicId": document.get("nearRtRicId")})

    def _backend_names(self) -> Tuple[str, ...]:
        manager = self.llm_manager
        reader = getattr(manager, "get_available_names", None)
        if reader is None:
            return ()
        try:
            return tuple(reader())
        except Exception:
            logger.debug("backend enumeration failed", exc_info=True)
            return ()

    # -- confirmation ------------------------------------------------------- #

    def _confirmed(self, spec) -> bool:
        if self.confirm is None:
            # Fail closed: an action that requires acknowledgement never
            # proceeds just because no dialog is available.
            return False
        outcome = self.confirm(spec)
        return bool(getattr(outcome, "confirmed", False))

    def _ask_confirmation(self, spec) -> ConfirmationOutcome:
        from .shell.confirm import ConfirmationDialog

        if self.window.root is None:
            return evaluate_confirmation(spec, acknowledged=False)
        return ConfirmationDialog(theme=self.theme).ask(self.window.root, spec)

    def _ask_open_path(self) -> str:
        if self.window.root is None:
            return ""
        from tkinter import filedialog

        return filedialog.askopenfilename(
            parent=self.window.root, title="Load experiment profile",
            filetypes=[("Profile", "*.json"), ("All files", "*")]) or ""

    def _ask_open_directory(self) -> str:
        if self.window.root is None:
            return ""
        from tkinter import filedialog

        return filedialog.askdirectory(
            parent=self.window.root, title="Load a recorded run") or ""

    def _ask_save_path(self) -> str:
        if self.window.root is None:
            return ""
        from tkinter import filedialog

        return filedialog.asksaveasfilename(
            parent=self.window.root, title="Save experiment profile",
            defaultextension=".json",
            initialfile=f"{self.profile.profile_id}.json") or ""

    # -- cosmetic state ----------------------------------------------------- #

    def gui_state_path(self) -> Path:
        return Path(self.profile.runs_root) / "gui-state.json"

    def save_gui_state(self) -> Optional[Path]:
        """Persist workspace and filter state.  Cosmetic only, never analysis."""
        try:
            path = self.gui_state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.window.gui_state(), indent=2,
                                       sort_keys=True) + "\n",
                            encoding="utf-8")
            return path
        except OSError:
            logger.debug("gui state not saved", exc_info=True)
            return None

    def restore_gui_state(self) -> None:
        path = self.gui_state_path()
        if not path.is_file():
            return
        try:
            self.window.restore_gui_state(
                json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            logger.debug("gui state not restored", exc_info=True)


def _assurance_of(result: Mapping[str, Any]) -> Optional[str]:
    """The S4 assurance verdict the episode recorded, or ``None``.

    Read from the last cycle only.  A missing verdict stays missing: it means
    the episode never reached S4, and inventing ``UNKNOWN`` there would make
    "we never asked" indistinguishable from "we asked and could not tell".
    """
    cycles = (result or {}).get("cycles") or []
    for cycle in reversed(list(cycles)):
        verdict = (cycle or {}).get("assurance_decision")
        if verdict is not None:
            return str(verdict)
    return None


def build_console(**kwargs: Any) -> OperatorConsole:
    """Factory used by the headless scenario and by evidence scripts.

    ``main.py`` has its own ``build_console`` - the deployed composition, which
    fixes what a default console is handed (nothing) rather than passing
    keywords through.  This one stays open, because a caller that is entitled
    to hand over a runtime has to be able to.
    """
    return OperatorConsole(**kwargs)


__all__ = ["DEFAULT_MODE", "OperatorConsole", "build_console"]
