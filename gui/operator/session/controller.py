"""The session controller: the console's only writer of session state.

Everything that changes what an operator sees goes through here, and everything
that leaves here goes onto the :class:`~gui.operator.viewmodel.bus.StateBus`.
That is what keeps the Tk thread free of I/O: the controller runs on workers,
publishes, and the window drains.

What ``start`` means
--------------------
Verify readiness, open a run directory, begin recording, and enable the
intent/policy/measurement/evidence workflow.  It does **not** start a Core, a
gNB, a UE or a USRP, and there is no method here that could.

What a non-success run means
----------------------------
``disposition`` has five values and only ``COMPLETED`` is a success.  ``ABORTED``,
``FAILED`` and ``INTERRUPTED`` all keep every artifact captured so far and all
render as non-success, which is how phaseB_task.md section 9's "preserve the raw
evidence and the progress point, and never report the result as success" is
delivered mechanically rather than by discipline.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .. import status as st
from ..shell.confirm import spec_for
from ..store import session_store as store_module
from ..viewmodel.bus import StateBus
from ..viewmodel.types import (
    CalibrationView, ComponentStatusView, ConfirmationSpec, CorrelationTraceView,
    DecisionView, DeploymentProvenanceView, IntentRowView, MetricAvailabilityView,
    PreflightCheckView, ReadinessSegmentView, SessionState, TimelineEventView,
)
from .preflight import PreflightRunner, blocking_failures, preflight_summary
from .profile import ExperimentProfile

logger = logging.getLogger("gui.operator.session")

#: The store is written by a worker; the recording indicator must never claim a
#: byte the store did not accept, so it is refreshed from the store's own count
#: rather than from the controller's intent to write.
#:
#: Before a session exists the console is ``DISCONNECTED``: it has opened no
#: source, contacted nothing and is observing nothing.  Claiming REPLAY there
#: would be claiming to be reading a recording that has not been loaded.
_DEFAULT_MODE: str = "DISCONNECTED"


class SessionError(RuntimeError):
    """A session lifecycle request that cannot be honoured."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class SessionController:
    """Owns the run lifecycle, the event sequence and the published state.

    Thread-safe by construction: workers call it, the Tk thread only reads what
    it published.
    """

    def __init__(self, bus: StateBus, *,
                 profile: Optional[ExperimentProfile] = None,
                 store_factory: Optional[Callable[..., Any]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], str] = utc_now) -> None:
        self.bus = bus
        self._lock = threading.RLock()
        self._profile = profile
        self._store_factory = store_factory or store_module.SessionStore.create
        self._clock = clock
        self._now = now

        self._store: Any = None
        self._run_id: Optional[str] = None
        self._mode: str = _DEFAULT_MODE
        self._mode_evidence: Mapping[str, Any] = {}
        self._disposition: str = "IDLE"
        self._started_monotonic: Optional[float] = None
        self._started_at: Optional[str] = None
        self._recording = False
        self._recorded_bytes = 0
        self._seq = 0

        self._components: Tuple[ComponentStatusView, ...] = ()
        self._readiness: Tuple[ReadinessSegmentView, ...] = ()
        self._llm_backends: Tuple[Any, ...] = ()
        self._active_backend: Optional[str] = None
        self._intents: Tuple[IntentRowView, ...] = ()
        self._metrics: Tuple[MetricAvailabilityView, ...] = ()
        self._decision: Optional[DecisionView] = None
        self._correlation_trace: Optional[CorrelationTraceView] = None
        self._deployment_provenance: Optional[DeploymentProvenanceView] = None
        self._calibration: Optional[CalibrationView] = None
        self._timeline: List[TimelineEventView] = []
        self._preflight: Tuple[PreflightCheckView, ...] = ()
        self._last_warning: Optional[TimelineEventView] = None
        self._pending_confirmation: Optional[ConfirmationSpec] = None
        self._workers: List[threading.Thread] = []
        self._stop_requested = threading.Event()

    # -- profile ------------------------------------------------------------ #

    @property
    def profile(self) -> Optional[ExperimentProfile]:
        with self._lock:
            return self._profile

    def set_profile(self, profile: Optional[ExperimentProfile]) -> None:
        with self._lock:
            if self._disposition == "RUNNING":
                raise SessionError(
                    "the profile cannot change while a run is recording; the "
                    "config snapshot would no longer describe the run")
            self._profile = profile
        # record_event builds and publishes the state after the change, so a
        # second publish here would only paint the same snapshot twice.
        self.record_event(lane="SESSION", kind="PROFILE_LOADED",
                          title=profile.profile_id if profile else "none")

    def load_profile(self, path) -> ExperimentProfile:
        profile = ExperimentProfile.load(path)
        self.set_profile(profile)
        return profile

    # -- preflight ---------------------------------------------------------- #

    def preflight(self, **kwargs: Any) -> Tuple[PreflightCheckView, ...]:
        """Run the read-only checks and publish the outcome.  Worker thread."""
        results = PreflightRunner(self.profile, **kwargs).run()
        with self._lock:
            self._preflight = results
        self.record_event(lane="SESSION", kind="PREFLIGHT_COMPLETE",
                          title=preflight_summary(results),
                          severity="WARNING" if blocking_failures(results)
                          else "INFO")
        return results

    @property
    def preflight_results(self) -> Tuple[PreflightCheckView, ...]:
        with self._lock:
            return self._preflight

    # -- lifecycle ---------------------------------------------------------- #

    def start(self, *, mode: str, mode_evidence: Mapping[str, Any],
              runs_root: Optional[str] = None,
              config_snapshot: Optional[Mapping[str, Any]] = None,
              require_preflight: bool = True) -> str:
        """Open a run directory and begin the workflow.  Worker thread.

        ``mode`` comes from the source, never from the operator: there is no
        control anywhere in this console that turns a Replay run into a LIVE
        one, because that is the single most damaging thing this design can get
        wrong.
        """
        with self._lock:
            if self._disposition == "RUNNING":
                raise SessionError(f"run {self._run_id} is already recording")
            profile = self._profile
            if mode not in store_module.MODES:
                raise SessionError(
                    f"unknown mode {mode!r}; expected one of "
                    f"{', '.join(store_module.MODES)}")
            if require_preflight:
                if not self._preflight:
                    raise SessionError("run Preflight before starting a session")
                blocking = blocking_failures(self._preflight)
                if blocking:
                    raise SessionError(
                        "preflight is blocking: "
                        + "; ".join(f"{v.check_id} {v.reason}" for v in blocking))
            root = runs_root or (profile.runs_root if profile else None)
            if not root:
                raise SessionError("no runs root is configured")
            snapshot = dict(config_snapshot or {})
            if profile is not None and "profile" not in snapshot:
                snapshot["profile"] = profile.to_dict()

        # A new session starts on a clean screen.  Everything the previous
        # session published is dropped here, before this one's first event, so
        # no row, event or graph can be carried across a mode change.
        self.reset_session_state(
            reason=f"starting a new {mode} session")

        store = self._store_factory(
            root, mode=mode, mode_evidence=dict(mode_evidence),
            profile_id=profile.profile_id if profile else None,
            config_snapshot=snapshot)

        # A store that cannot identify itself is not a store that can be said to
        # be recording.  Read its identity defensively and downgrade honestly
        # rather than letting a half-built store take the window down.
        identified = _store_attr(store, "run_id")
        with self._lock:
            self._store = store
            self._run_id = identified
            self._mode = _store_attr(store, "mode") or mode
            self._mode_evidence = dict(mode_evidence)
            self._disposition = "RUNNING"
            self._started_monotonic = self._clock()
            self._started_at = self._now()
            self._recording = bool(profile.recording) if profile else True
            self._recorded_bytes = 0
            self._stop_requested.clear()
            run_id = self._run_id or ""

        if identified is None:
            self._mark_not_recording(
                "the session store did not report a run id; nothing is being "
                "recorded for this session")

        self.record_event(
            lane="SESSION", kind="SESSION_STARTED", title=f"run {run_id}",
            detail={"mode": self._mode, "modeEvidence": dict(mode_evidence),
                    "meaning": "readiness verified and the intent/policy/"
                               "evidence workflow enabled; no network element "
                               "was started"})
        return run_id

    def abort_confirmation(self) -> ConfirmationSpec:
        """The typed confirmation an abort requires, with its real targets."""
        with self._lock:
            run_id = self._run_id or "no run"
            elapsed = self.elapsed_s
            events = len(self._timeline)
            intents = tuple(row.intent_id for row in self._intents)
        return spec_for(
            "C-SESSION-ABORT", title=f"Abort run {run_id}",
            targets=(f"run {run_id}",
                     f"elapsed {elapsed:.0f}s" if elapsed is not None
                     else "elapsed unknown",
                     f"{events} recorded event(s)",
                     f"active intents: {', '.join(intents) or 'none'}"),
            effects=("the run is finalized with disposition ABORTED",
                     "every artifact captured so far is preserved",
                     "the run is never reported as a success",
                     "active intents and their policies are NOT withdrawn by "
                     "this action"))

    def stop(self, disposition: str = "COMPLETED") -> Mapping[str, Any]:
        """Finalize the run.  Worker thread.

        A finalize that fails still leaves the console in a non-running state
        with the failure recorded - an operator must never be left believing a
        run is still capturing when it is not.
        """
        if disposition not in store_module.DISPOSITIONS:
            raise SessionError(f"unknown disposition {disposition!r}")
        with self._lock:
            store = self._store
            if store is None or self._disposition != "RUNNING":
                raise SessionError("no session is running")
            self._stop_requested.set()
        manifest: Mapping[str, Any] = {}
        error: Optional[str] = None
        try:
            manifest = store.finalize(disposition) or {}
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("finalize failed")
        with self._lock:
            self._disposition = disposition
            self._recording = False
            self._store = store
        if error:
            self.record_event(lane="SESSION", kind="SESSION_FINALIZE_FAILED",
                              severity="ERROR", title=error)
        else:
            self.record_event(
                lane="SESSION", kind="SESSION_FINALIZED",
                severity="INFO" if disposition in
                store_module.SUCCESS_DISPOSITIONS else "WARNING",
                title=f"disposition {disposition}")
        return manifest

    def abort(self) -> Mapping[str, Any]:
        """Finalize as ABORTED.  Requires the typed confirmation upstream."""
        return self.stop("ABORTED")

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    # -- recording ---------------------------------------------------------- #

    def record_event(self, *, lane: str, kind: str, severity: str = "INFO",
                     title: str = "", component: Optional[str] = None,
                     intent_id: Optional[str] = None,
                     policy_id: Optional[str] = None,
                     episode_id: Optional[str] = None,
                     evidence_id: Optional[str] = None,
                     correlation_id: Optional[str] = None,
                     origin: str = "OBSERVED",
                     derivation: Optional[str] = None,
                     t_utc: Optional[str] = None,
                     t_rel_s: Optional[float] = None,
                     detail: Optional[Mapping[str, Any]] = None
                     ) -> TimelineEventView:
        """Append one event to the timeline, the store and the bus.

        Never raises: a failure to record an event must not be able to abort the
        experiment that produced it.  A store write that fails is itself
        recorded, on the bus, as an error.
        """
        with self._lock:
            self._seq += 1
            seq = self._seq
            run_id = self._run_id
            store = self._store
            elapsed = self.elapsed_s
        event = TimelineEventView(
            seq=seq, lane=lane, kind=kind, severity=severity, origin=origin,
            derivation=derivation, t_utc=t_utc or self._now(),
            t_rel_s=t_rel_s if t_rel_s is not None else elapsed,
            title=title, component=component, run_id=run_id,
            intent_id=intent_id, policy_id=policy_id, episode_id=episode_id,
            evidence_id=evidence_id, correlation_id=correlation_id,
            detail=dict(detail or {}))
        with self._lock:
            self._timeline.append(event)
            if len(self._timeline) > 20000:
                del self._timeline[:len(self._timeline) - 20000]
            if severity in ("WARNING", "ERROR", "CRITICAL"):
                self._last_warning = event
        self.bus.publish("timeline", event)
        if severity in ("WARNING", "ERROR", "CRITICAL"):
            self.bus.publish("warning", event)
        if store is not None and self._recording:
            try:
                store.append_event(_event_record(event))
            except NotImplementedError:
                # The store body lands with track T3.  Until then the console
                # still runs; it simply cannot claim to be recording.
                self._mark_not_recording(
                    "the session store implementation is not present")
            except Exception as exc:
                logger.exception("event append failed")
                self.bus.publish("warning", replace(
                    event, kind="RECORDING_FAILED", severity="ERROR",
                    title=f"{type(exc).__name__}: {exc}"))
        # Republish the state so the timeline and the "latest alert" cell move
        # with the event that caused them.  Events are operator-scale, not
        # sample-scale - telemetry never comes through here - so one state
        # snapshot per event is affordable, and the alternative (a second
        # timeline held by the renderer) would be a copy that can disagree.
        self.publish_state()
        return event

    def _mark_not_recording(self, reason: str) -> None:
        with self._lock:
            if not self._recording:
                return
            self._recording = False
        logger.warning("recording disabled: %s", reason)
        self.bus.publish("warning", TimelineEventView(
            seq=0, lane="SESSION", kind="RECORDING_UNAVAILABLE",
            severity="WARNING", title=reason, t_utc=self._now()))

    # -- state -------------------------------------------------------------- #

    def set_components(self, components: Sequence[ComponentStatusView],
                       readiness: Sequence[ReadinessSegmentView] = ()) -> None:
        with self._lock:
            self._components = tuple(components)
            if readiness:
                self._readiness = tuple(readiness)
        self.bus.publish("health", {"components": tuple(components),
                                    "readiness": tuple(readiness)})
        self.publish_state()

    def set_intents(self, intents: Sequence[IntentRowView]) -> None:
        with self._lock:
            self._intents = tuple(intents)
        self.publish_state()

    def set_llm_backends(self, backends: Sequence[Any],
                         active: Optional[str] = None) -> None:
        """Publish the proposer inventory the header and the model bar read.

        The rows come from the existing ``LLMBackendManager`` through
        ``LlmRegistry``; nothing here decides availability, and no credential
        value can reach this path because a row carries reference names only.
        """
        with self._lock:
            self._llm_backends = tuple(backends)
            self._active_backend = active
        self.bus.publish("llm", tuple(backends))
        self.publish_state()

    @property
    def llm_backends(self) -> Tuple[Any, ...]:
        with self._lock:
            return self._llm_backends

    def mark_disconnected(self) -> None:
        """Return the mode badge to DISCONNECTED after everything detached.

        Refused while a run is recording: a session in progress has a mode, and
        the badge is what tells an operator whether what they are watching is a
        radio or a recording.  The finished run keeps its own mode in its
        manifest, its export and the Analysis workspace - this only stops the
        header claiming a source the console no longer holds.
        """
        with self._lock:
            if self._disposition == "RUNNING":
                raise SessionError(
                    "a session is recording; stop it before disconnecting")
            self._mode = _DEFAULT_MODE
            self._mode_evidence = {}
        self.publish_state()

    def reset_session_state(self, *, reason: str) -> None:
        """Drop everything the previous session put on screen.

        Called when a session starts.  Without it the next session inherits the
        previous one's timeline, intent rows, decision, metric availability and
        topology - so a Replay of a recording would show a Live session's events
        and a reader could not tell which session produced which row.  Preflight
        results deliberately survive: they describe the console's readiness for
        the session about to start, not the session that ended.
        """
        with self._lock:
            self._components = ()
            self._readiness = ()
            self._intents = ()
            self._metrics = ()
            self._decision = None
            self._correlation_trace = None
            self._calibration = None
            self._timeline = []
            self._last_warning = None
            self._pending_confirmation = None
            self._recorded_bytes = 0
            self._seq = 0
        self.bus.publish("health", {"components": (), "readiness": ()})
        self.bus.publish("decision", None)
        self.record_event(lane="SESSION", kind="SESSION_STATE_CLEARED",
                          title=reason,
                          detail={"cleared": ["timeline", "intents", "decision",
                                              "correlationTrace", "calibration",
                                              "metrics", "components", "readiness"]})

    def set_decision(self, decision: Optional[DecisionView], *,
                     calibration: Optional[CalibrationView] = None) -> None:
        """Publish the current decision, and its calibration when there is one.

        Added at integration for the same reason as :meth:`set_metrics`:
        ``SessionState.decision`` was declared and nothing filled it, so the
        Demo View's Operator Intent and S0-S6 cards and the Intent & Decision
        panel all rendered empty in an assembled console while the decision sat
        on the bus.
        """
        with self._lock:
            self._decision = decision
            if calibration is not None:
                self._calibration = calibration
        self.bus.publish("decision", decision)

    def set_correlation_trace(self, trace: Optional[CorrelationTraceView]) -> None:
        """Publish the correlation trace for the episode that just completed.

        Mirrors :meth:`set_decision`: ``SessionState.correlation_trace`` needs a
        caller because the store is otherwise never told what to render, and a
        workspace that reads only ``SessionState`` would show nothing.
        """
        with self._lock:
            self._correlation_trace = trace
        self.publish_state()

    def set_deployment_provenance(
            self, provenance: Optional[DeploymentProvenanceView]) -> None:
        """Publish which deployed release the bound deployment composes against.

        ``None`` on disconnect: a provenance view naming a deployment the
        console no longer holds would misdescribe the console's own state,
        the same reason :meth:`mark_disconnected` returns the mode badge.
        """
        with self._lock:
            self._deployment_provenance = provenance
        self.publish_state()

    def set_metrics(self, metrics: Sequence[MetricAvailabilityView]) -> None:
        """Publish the metric-availability rows.

        Added at integration.  ``SessionState.metrics`` was declared by the
        design step and no track filled it, so the Demo View's headline KPI card
        had nothing to read.  Availability rows carry their value, their quality
        and their reason together - a card can then show the number *or* say
        what is missing, and never a number with neither.
        """
        with self._lock:
            self._metrics = tuple(metrics)
        self.publish_state()

    def set_pending_confirmation(self,
                                 spec: Optional[ConfirmationSpec]) -> None:
        with self._lock:
            self._pending_confirmation = spec
        self.publish_state()

    @property
    def elapsed_s(self) -> Optional[float]:
        started = self._started_monotonic
        return None if started is None else max(0.0, self._clock() - started)

    @property
    def timeline(self) -> Tuple[TimelineEventView, ...]:
        with self._lock:
            return tuple(self._timeline)

    @property
    def run_id(self) -> Optional[str]:
        with self._lock:
            return self._run_id

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    @property
    def disposition(self) -> str:
        with self._lock:
            return self._disposition

    def state(self) -> SessionState:
        """The snapshot every workspace renders from."""
        with self._lock:
            profile = self._profile
            statuses = {view.element_id: view.status for view in self._components}
            summary = st.health_summary(statuses)
            return SessionState(
                run_id=self._run_id, mode=self._mode,
                disposition=self._disposition,
                profile_id=profile.profile_id if profile else None,
                profile_valid=None if profile is None else profile.is_valid,
                started_at=self._started_at, elapsed_s=self.elapsed_s,
                recording=self._recording, recorded_bytes=self._recorded_bytes,
                health=summary["worst"], health_counts=summary["counts"],
                components=self._components, readiness=self._readiness,
                intents=self._intents, metrics=self._metrics,
                llm_backends=self._llm_backends,
                active_backend=self._active_backend,
                decision=self._decision, correlation_trace=self._correlation_trace,
                deployment_provenance=self._deployment_provenance,
                calibration=self._calibration,
                timeline=tuple(self._timeline[-2000:]),
                last_warning=self._last_warning,
                pending_confirmation=self._pending_confirmation,
                dropped_updates=self.bus.dropped())

    def publish_state(self) -> None:
        """Publish the current state.  Callable from any thread."""
        try:
            self.bus.publish("session", self.state())
        except Exception:                                 # pragma: no cover
            logger.exception("state publish failed")

    # -- workers ------------------------------------------------------------ #

    def run_in_worker(self, name: str, call: Callable[[], Any], *,
                      on_error: Optional[Callable[[BaseException], None]] = None
                      ) -> threading.Thread:
        """Run ``call`` off the Tk thread.

        The console never performs I/O, a coordinator episode, or an export on
        the thread that repaints.  This is the one sanctioned way to leave it.
        """
        def _run() -> None:
            try:
                call()
            except Exception as exc:
                logger.exception("worker %s failed", name)
                if on_error is not None:
                    try:
                        on_error(exc)
                    except Exception:                     # pragma: no cover
                        logger.exception("worker error handler failed")
                else:
                    self.record_event(lane="WARNING", kind="WORKER_FAILED",
                                      severity="ERROR",
                                      title=f"{name}: {type(exc).__name__}: {exc}")

        thread = threading.Thread(target=_run, name=f"aic-gui-{name}",
                                  daemon=True)
        with self._lock:
            self._workers = [t for t in self._workers if t.is_alive()]
            self._workers.append(thread)
        thread.start()
        return thread

    def join_workers(self, timeout: float = 5.0) -> None:
        for thread in tuple(self._workers):
            thread.join(timeout=timeout)


def _store_attr(store: Any, name: str) -> Any:
    """Read ``store.name``, treating any failure as "not reported".

    The store body is another track's file and may be a signature-only stub
    during a parallel build.  A property that raises is a fact about the store,
    not a reason for the console to stop.
    """
    try:
        return getattr(store, name)
    except Exception:
        return None


def _event_record(event: TimelineEventView) -> Dict[str, Any]:
    """The store record for one timeline event.

    Explicit rather than a blanket ``asdict`` so a future view-model field
    cannot silently start being persisted into research evidence.
    """
    return {
        "seq": event.seq, "lane": event.lane, "kind": event.kind,
        "severity": event.severity, "origin": event.origin,
        "derivation": event.derivation, "tUtc": event.t_utc,
        "tRelS": event.t_rel_s, "title": event.title,
        "component": event.component, "runId": event.run_id,
        "intentId": event.intent_id, "policyId": event.policy_id,
        "episodeId": event.episode_id, "evidenceId": event.evidence_id,
        "correlationId": event.correlation_id, "detail": dict(event.detail),
    }


__all__ = ["SessionController", "SessionError", "utc_now"]
