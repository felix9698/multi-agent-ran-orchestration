"""The section 10 ten-step operator flow, driven end to end over a Replay source.

Owner: **integration**.  ``phaseB_task.md`` section 10 lists ten steps the
console must be able to carry out from beginning to end, and requires that a
development machine with no live backend reproduce them over Replay *without
presenting the result as Live*.  This module is that driver.

It exists at integration rather than in a track because it is the only piece
that needs all four of them at once: T1's shell, controller and preflight, T2's
decision projections, T3's Replay adapters, session store and export pipeline,
and T4's Demo View and gates.  T4 left the test in place and honestly skipped
it, because no track owned a hook that crossed those lines.

Design rules this driver obeys, and why each matters
----------------------------------------------------
**It drives the real console, not a parallel copy of it.**  Every step goes
through ``OperatorConsole.handle_action`` or the controller method the button
routes to, including the confirmation gate and the worker thread.  A driver that
called the store directly would prove that the store works, which nobody
doubted, and nothing about whether the console can be operated.

**It is headless by construction.**  Nothing here touches the toolkit.  A caller
that *has* a window passes ``on_step``; the driver calls it after each step so
the caller can repaint and screenshot.  The scenario itself neither knows nor
cares whether a display exists.

**A Replay session is a Replay session.**  The mode comes from the attached
source through ``OperatorConsole.session_mode_evidence``; there is no argument
here that sets it.  Everything the driver replays into the session store is
recorded with its source run id and, where the record was inferred rather than
observed, ``origin=DERIVED`` with the derivation stated.

**The recorded decision is not attributed to the typed intent.**  The operator
really does type and submit an intent - that part is not simulated - but in
Replay the S0-S6 path and its outcome come from the *recording*, and the
submitted text did not cause them.  Every replayed decision record therefore
carries ``replayOf`` naming the source run and episode, and the timeline event
says so in words.  Blurring that line would make the console's most important
screen a fabrication.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from ..shell.confirm import evaluate_confirmation

logger = logging.getLogger("gui.operator.session.scenario")

#: The ten steps of phaseB_task.md section 10, in order, with the identifier
#: each result carries.  Written out so a partial run still says which step it
#: reached and which it never attempted.
STEPS: Tuple[Tuple[str, str], ...] = (
    ("S10-01", "Load the experiment profile"),
    ("S10-02", "Check integration status and run Preflight"),
    ("S10-03", "Start the experiment session"),
    ("S10-04", "Submit a natural-language intent"),
    ("S10-05", "Read the S0-S6 path and the LLM analysis"),
    ("S10-06", "Follow policy, xApp, O1 and DME progress"),
    ("S10-07", "Read live KPI and event annotations"),
    ("S10-08", "Stop and finalize the session"),
    ("S10-09", "Re-open the finalized run"),
    ("S10-10", "Export raw and processed data and the paper figure"),
)


class ScenarioError(RuntimeError):
    """A step that could not be carried out.  Carries the step it failed on."""

    def __init__(self, step_id: str, detail: str) -> None:
        super().__init__(f"{step_id}: {detail}")
        self.step_id = step_id
        self.detail = detail


@dataclass(frozen=True)
class StepResult:
    """One of the ten steps, with the evidence that it happened."""

    number: int
    step_id: str
    title: str
    ok: bool
    evidence: Mapping[str, Any] = field(default_factory=dict)
    detail: str = ""


@dataclass(frozen=True)
class ScenarioResult:
    """The whole flow, and what it produced."""

    steps: Tuple[StepResult, ...] = ()
    source_run_dir: Optional[str] = None
    session_run_dir: Optional[str] = None
    session_run_id: Optional[str] = None
    mode: Optional[str] = None
    source_mode: Optional[str] = None
    export_dir: Optional[str] = None
    figures: Tuple[Mapping[str, Any], ...] = ()

    @property
    def ok(self) -> bool:
        return len(self.steps) == len(STEPS) and all(s.ok for s in self.steps)

    def step(self, step_id: str) -> Optional[StepResult]:
        return next((s for s in self.steps if s.step_id == step_id), None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "mode": self.mode,
            "sourceMode": self.source_mode,
            "sessionRunId": self.session_run_id,
            "sessionRunDir": self.session_run_dir,
            "sourceRunDir": self.source_run_dir,
            "exportDir": self.export_dir,
            "figures": [dict(f) for f in self.figures],
            "steps": [{"number": s.number, "stepId": s.step_id,
                       "title": s.title, "ok": s.ok, "detail": s.detail,
                       "evidence": dict(s.evidence)} for s in self.steps],
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _episode_as_decision_input(episode: Mapping[str, Any], *,
                               intent_text: str) -> Dict[str, Any]:
    """A stored ``EpisodeTrace`` in the shape the decision projection reads.

    A straight rename, field by field, and nothing else: no default, no
    inference, no value the record did not carry.  ``project_decision`` reads
    both spellings for the fields it knows about; this fills in the rest and is
    the one place the two vocabularies meet.
    """
    document = dict(episode)
    document.setdefault("intent_text", intent_text)
    for stored, projected in (("episodeId", "episode_id"),
                              ("fsmPath", "fsm_history"),
                              ("terminalOutcome", "terminal_outcome"),
                              ("terminalReason", "terminal_reason"),
                              ("negotiationRounds", "negotiation_rounds"),
                              ("rolledBack", "rolled_back"),
                              ("latencyMs", "latency_ms"),
                              ("hasConflict", "has_conflict"),
                              ("parseMs", "parse_ms")):
        if stored in episode and projected not in document:
            document[projected] = episode[stored]
    return document


class TenStepReplayScenario:
    """Drive the ten operator steps over one recorded source.

    ``console`` is a real :class:`~gui.operator.app.OperatorConsole`.  The driver
    installs an auto-acknowledging confirmation callback - the confirmation
    *rule* still runs, through ``evaluate_confirmation``; only the click is
    supplied - and restores whatever was there when it is done.
    """

    def __init__(self, console: Any, *, source_path: Any,
                 profile_path: Optional[Any] = None,
                 intent_text: str = "Keep UE2 downlink throughput above the "
                                    "session goal while UE1 keeps its share",
                 session_id: Optional[str] = None,
                 export_dir: Optional[Any] = None,
                 on_step: Optional[Callable[[StepResult], None]] = None,
                 worker_timeout: float = 60.0) -> None:
        self.console = console
        self.controller = console.controller
        self.source_path = Path(source_path)
        self.profile_path = Path(profile_path) if profile_path else None
        self.intent_text = intent_text
        self.session_id = session_id
        self.export_dir = Path(export_dir) if export_dir else None
        self.on_step = on_step
        self.worker_timeout = worker_timeout

        self.source_store: Any = None
        self.session_store: Any = None
        self.reopened: Any = None
        self.run_view: Any = None
        self._episode: Dict[str, Any] = {}
        self._decision: Any = None
        self._replayed_events: List[Mapping[str, Any]] = []
        self._replayed_samples: List[Mapping[str, Any]] = []
        self._figures: List[Mapping[str, Any]] = []
        self._steps: List[StepResult] = []

    # -- plumbing ---------------------------------------------------------- #

    def _settle(self) -> None:
        """Let the console's workers finish and its bus drain."""
        self.controller.join_workers(timeout=self.worker_timeout)
        try:
            self.console.bus.drain(budget=100000)
        except Exception:                                 # pragma: no cover
            logger.debug("bus drain during scenario", exc_info=True)

    def _record(self, number: int, ok: bool, evidence: Mapping[str, Any],
                detail: str = "") -> StepResult:
        step_id, title = STEPS[number - 1]
        result = StepResult(number=number, step_id=step_id, title=title, ok=ok,
                            evidence=dict(evidence), detail=detail)
        self._steps.append(result)
        if self.on_step is not None:
            try:
                self.on_step(result)
            except Exception:                             # pragma: no cover
                logger.exception("scenario on_step hook failed")
        if not ok:
            raise ScenarioError(step_id, detail or "step did not complete")
        return result

    def _source_ladder(self) -> Optional[Dict[str, Any]]:
        """The recorded readiness ladder, keyed the way the strip reads it.

        A rename of ``segmentId`` into the key position and nothing else.  A
        source with no ladder returns ``None``, which the strip renders as
        UNKNOWN - never as confirmed.
        """
        if self.source_store is None:
            return None
        readiness = (self.source_store.read_summary() or {}).get("readiness")
        segments = (readiness or {}).get("segments")
        if not isinstance(segments, list) or not segments:
            return None
        ladder: Dict[str, Any] = {}
        for segment in segments:
            if not isinstance(segment, Mapping):
                continue
            name = segment.get("segmentId")
            if name:
                ladder[str(name)] = dict(segment)
        return ladder or None

    def _store(self) -> Any:
        store = getattr(self.controller, "_store", None)
        if store is None:
            raise ScenarioError("S10-03", "the session has no store")
        return store

    # -- the ten steps ------------------------------------------------------ #

    def step_01_load_profile(self) -> StepResult:
        if self.profile_path is not None:
            self.console.handle_action("profile_load", str(self.profile_path))
        else:
            self.console.handle_action("profile_new")
        self._settle()
        profile = self.console.profile
        loaded = self.controller.profile
        ok = loaded is not None and loaded.profile_id == profile.profile_id
        return self._record(1, ok, {
            "profilePath": str(self.profile_path) if self.profile_path else None,
            "profileId": profile.profile_id,
            "runsRoot": profile.runs_root,
            "recording": profile.recording,
            "controllerProfileId": getattr(loaded, "profile_id", None),
        }, "" if ok else "the controller did not take the loaded profile")

    def step_02_preflight(self) -> StepResult:
        from .preflight import blocking_failures, preflight_summary

        self.console.handle_action("preflight")
        self._settle()
        results = self.controller.preflight_results
        blocking = blocking_failures(results)
        ok = bool(results) and not blocking
        return self._record(2, ok, {
            "summary": preflight_summary(results),
            "checks": [{"id": v.check_id, "status": v.status,
                        "boundary": v.boundary, "reason": v.reason,
                        "detail": v.detail} for v in results],
            "blocking": [v.check_id for v in blocking],
        }, "" if ok else "Preflight is blocking: "
           + ", ".join(v.check_id for v in blocking))

    def step_03_start_session(self) -> StepResult:
        # Attaching the recorded source is what makes this a Replay session.
        # It runs on a worker because it reads a directory and writes a run.
        thread = self.controller.run_in_worker(
            "attach-replay-source",
            lambda: setattr(self, "source_store",
                            self.console.attach_replay_source(
                                self.source_path, session_id=self.session_id)))
        thread.join(timeout=self.worker_timeout)
        self._settle()
        if self.source_store is None:
            raise ScenarioError("S10-03",
                                f"the recorded source at {self.source_path} "
                                "did not load")

        # The recorded source carries the O1 readiness ladder; project it so
        # the readiness strip shows the real chain rather than "no evidence".
        self.console.refresh_status(readiness_ladder=self._source_ladder())

        mode, evidence = self.console.session_mode_evidence()
        self.console.handle_action("start")
        self._settle()
        self.session_store = self._store()
        state = self.controller.state()
        ok = (state.disposition == "RUNNING" and bool(self.controller.run_id)
              and state.mode == "REPLAY" and not state.is_live)
        return self._record(3, ok, {
            "mode": state.mode,
            "modeEvidence": evidence,
            "isLive": state.is_live,
            "runId": self.controller.run_id,
            "runDir": str(self.session_store.run_dir),
            "recording": state.recording,
            "sourceRunId": (self.console.replay_source or {}).get("sourceRunId"),
            "sourceMode": (self.console.replay_source or {}).get("sourceMode"),
            "startMeaning": "readiness verified and the intent/policy/evidence "
                            "workflow enabled; no network element was started",
        }, "" if ok else f"the session did not start in REPLAY ({state.mode}, "
                         f"{state.disposition})")

    def step_04_submit_intent(self) -> StepResult:
        previous_confirm = self.console.confirm
        previous_submitter = self.console.intent_submitter
        seen: List[Any] = []

        def auto_confirm(spec):
            seen.append(spec)
            return evaluate_confirmation(spec, acknowledged=True,
                                         typed=spec.typed_phrase)

        self.console.confirm = auto_confirm
        self.console.intent_submitter = self._replay_intent_submitter
        try:
            self.console.handle_action("submit", self.intent_text)
            self._settle()
        finally:
            self.console.confirm = previous_confirm
            self.console.intent_submitter = previous_submitter

        kinds = [event.kind for event in self.controller.timeline]
        ok = ("INTENT_SUBMITTED" in kinds and bool(seen)
              and bool(self._episode))
        return self._record(4, ok, {
            "intentText": self.intent_text,
            "confirmationShown": bool(seen),
            "confirmationTargets": list(seen[0].targets) if seen else [],
            "confirmationEffects": list(seen[0].effects) if seen else [],
            "replayedEpisodeId": self._episode.get("episodeId"),
            "attribution": "the operator's submission is real; the S0-S6 path "
                           "and outcome below are replayed from the recording "
                           "and were not produced by this text",
        }, "" if ok else "the intent submission did not reach the timeline")

    def _replay_intent_submitter(self, text: str) -> None:
        """What ``submit`` does in a Replay session.

        It does **not** call the coordinator: nothing is being decided now.  It
        selects the recorded episode, records it against this session with its
        provenance, and hands it to the same projection the live path uses - so
        the Intent & Decision workspace has one code path, not two.
        """
        from ..sources.live import project_decision, project_intent_row

        episodes = list(self.source_store.read_episodes())
        if not episodes:
            raise ScenarioError(
                "S10-04", f"the recorded source {self.source_store.run_id} "
                          "carries no decision episode to replay")
        self._episode = dict(episodes[0])
        document = _episode_as_decision_input(self._episode,
                                              intent_text=text)
        self._decision = project_decision(document, intent_text=text)

        store = self._store()
        replay_of = {"sourceRunId": self.source_store.run_id,
                     "sourceRunDir": str(self.source_store.run_dir),
                     "episodeId": self._episode.get("episodeId")}
        store.append_episode({**self._episode, "replayOf": replay_of})
        for cycle in self.source_store.read_cycles():
            store.append_cycle({**cycle, "replayOf": replay_of})
        for call in self.source_store.read_llm_calls():
            store.append_llm_call(dict(call))

        self.controller.record_event(
            lane="DECISION", kind="DECISION_REPLAYED",
            origin="DERIVED",
            derivation="the decision is the recorded episode being replayed; "
                       "the submitted intent text did not produce it",
            title=f"episode {self._episode.get('episodeId')} "
                  f"-> {self._decision.eq12_state or 'unknown'}",
            episode_id=self._episode.get("episodeId"),
            detail={"replayOf": replay_of,
                    "terminalOutcome": self._decision.terminal_outcome,
                    "eq12State": self._decision.eq12_state})

        row = project_intent_row(document, intent_text=text)
        self.controller.set_intents((row,))
        # Through the controller, not straight onto the bus: the workspaces
        # render SessionState, and a decision that only ever existed as a bus
        # payload left the Demo View's Operator Intent card empty.
        self.controller.set_decision(self._decision)

    def step_05_decision_path(self) -> StepResult:
        decision = self._decision
        if decision is None:
            raise ScenarioError("S10-05", "no decision was projected")
        fsm = [(s.stage_id, s.state) for s in decision.fsm_stages]
        llm = [(s.stage_id, s.state, s.duration_ms)
               for s in decision.llm_stages]
        visited = [sid for sid, state in fsm if state != "PENDING"]
        ok = bool(fsm) and bool(llm) and decision.eq12_state in (
            "Admitted", "NotAdmitted", "TechnicalFailsafe")
        return self._record(5, ok, {
            "episodeId": decision.episode_id,
            "fsmStages": [{"id": sid, "state": state} for sid, state in fsm],
            "fsmVisited": visited,
            "llmStages": [{"id": sid, "state": state, "durationMs": ms}
                          for sid, state, ms in llm],
            "terminalOutcome": decision.terminal_outcome,
            "eq12State": decision.eq12_state,
            "feasible": decision.feasible,
            "rawConfidence": decision.raw_confidence,
            "calibratedProbability": decision.calibrated_probability,
            "thetaStar": decision.theta_star,
            "unavailableNote": "a capture carries no confidence, no theta* and "
                               "no per-stage LLM latency; those read Unavailable "
                               "rather than as a number",
        }, "" if ok else "the projected decision has no Eq.12 terminal state")

    def step_06_policy_and_evidence(self) -> StepResult:
        """Replay the recorded R1/A1/O1/DME timeline into this session."""
        store = self._store()
        lanes: Dict[str, int] = {}
        replay_of = {"sourceRunId": self.source_store.run_id}
        for event in self.source_store.read_events():
            record = dict(event)
            record["replayOf"] = replay_of
            store.append_event(record)
            self._replayed_events.append(record)
            lane = str(record.get("lane") or "UNKNOWN")
            lanes[lane] = lanes.get(lane, 0) + 1
        self.controller.record_event(
            lane="SESSION", kind="TIMELINE_REPLAYED",
            origin="DERIVED",
            derivation="events copied from the recorded source, in the "
                       "source's own order, with their recorded times",
            title=f"{len(self._replayed_events)} recorded events",
            detail={"lanes": dict(sorted(lanes.items())), "replayOf": replay_of})
        self._settle()
        summary = self.source_store.read_summary()
        readiness = summary.get("readiness") or {}
        ok = bool(self._replayed_events) and any(
            lane in lanes for lane in ("R1", "A1", "O1", "DME", "COORDINATOR"))
        return self._record(6, ok, {
            "eventCount": len(self._replayed_events),
            "lanes": dict(sorted(lanes.items())),
            "policyIds": list(self._episode.get("policyIds") or ()),
            "readinessSegments": [
                s.get("segment") if isinstance(s, Mapping) else s
                for s in (readiness.get("states") or readiness.get("segments")
                          or ())],
            "stateBeforeAfter": summary.get("stateBeforeAfter"),
        }, "" if ok else "no policy, evidence or coordinator lane was replayed")

    def step_07_kpi_and_annotations(self) -> StepResult:
        """Replay telemetry, publish availability, and place the annotations."""
        from ..export.figure_export import annotations_from_events
        from ..workspaces.analysis import AnalysisModel, RunView

        store = self._store()
        for sample in self.source_store.read_telemetry():
            record = dict(sample)
            store.append_telemetry(record)
            self._replayed_samples.append(record)
        index = dict(self.source_store.read_metric_index())
        store.write_metric_index(index)

        # The availability view the Demo View's headline card reads.  Built
        # from the run's own index, so an unsupported metric stays visible with
        # its reason instead of vanishing.
        model = AnalysisModel()
        model.add_run(RunView(
            run_id=store.run_id, mode=store.mode,
            disposition=self.controller.disposition, is_success=False,
            run_dir=str(store.run_dir),
            samples=tuple(self._replayed_samples), metric_index=index,
            events=tuple(self._replayed_events)), primary=True)
        metrics = tuple(model.metric_availability_views())
        self.controller.set_metrics(metrics)
        # And into the Analysis workspace, which renders its own model rather
        # than SessionState.
        self.console.show_run(store)
        self._settle()

        def x_of(event: Mapping[str, Any]) -> Optional[float]:
            value = event.get("tRelS")
            return float(value) if isinstance(value, (int, float)) else None

        annotations = annotations_from_events(self._replayed_events, x_of=x_of)
        annotatable = [e for e in self._replayed_events if e.get("annotatable")]
        measured = [m for m in metrics if m.status == "OK"
                    and m.last_value is not None]
        unsupported = [m for m in metrics if m.status != "OK"]
        ok = bool(self._replayed_samples) and bool(measured) and bool(unsupported)
        return self._record(7, ok, {
            "sampleCount": len(self._replayed_samples),
            "metricsMeasured": [{"metric": m.metric, "value": m.last_value,
                                 "unit": m.unit, "quality": m.last_value_quality,
                                 "scope": m.last_value_scope}
                                for m in measured],
            "metricsNotMeasured": [{"metric": m.metric, "status": m.status,
                                    "reason": m.reason, "gapId": m.gap_id}
                                   for m in unsupported],
            "annotatableEvents": [e.get("kind") for e in annotatable],
            "placedAnnotations": [a["kind"] for a in annotations],
            "annotationNote": "an annotatable event with no time on this axis "
                              "is skipped, never drawn at an invented position",
        }, "" if ok else "no measured KPI or no availability row was produced")

    def step_08_finalize(self) -> StepResult:
        run_dir = str(self._store().run_dir)
        self.console.handle_action("stop")
        self._settle()
        state = self.controller.state()
        summary = state.summary
        ok = (self.controller.disposition == "COMPLETED"
              and Path(run_dir, "manifest.json").is_file())
        return self._record(8, ok, {
            "runDir": run_dir,
            "disposition": self.controller.disposition,
            "isSuccess": getattr(summary, "is_success", None),
            "elapsedS": state.elapsed_s,
            "manifestWritten": Path(run_dir, "manifest.json").is_file(),
        }, "" if ok else f"the run finalized as {self.controller.disposition}")

    def step_09_reopen(self) -> StepResult:
        from ..store.session_store import SessionStore

        run_dir = self._steps[-1].evidence["runDir"]
        self.reopened = SessionStore.open(run_dir)
        self.run_view = self.console.show_run(self.reopened)
        view = self.run_view
        ok = (view.run_id == self.controller.run_id and view.mode == "REPLAY"
              and bool(view.samples) and bool(view.events)
              and bool(view.episodes))
        return self._record(9, ok, {
            "runId": view.run_id,
            "mode": view.mode,
            "disposition": view.disposition,
            "sampleCount": len(view.samples),
            "eventCount": len(view.events),
            "episodeCount": len(view.episodes),
            "metricsIndexed": sorted(view.metric_index),
            "dataIssues": [dict(i) for i in view.data_issues],
            "reopenNote": "read back from the directory after finalize; this "
                          "is the same path a restarted console takes",
        }, "" if ok else "the finalized run did not re-open with its content")

    def step_10_export(self) -> StepResult:
        from ..export.data_export import export_run
        from ..export.figure_export import (FigureSpec, annotations_from_events,
                                            export_figure, series_from_samples)

        destination = self.export_dir or (Path(self.reopened.run_dir) / "export")
        manifest = export_run(self.reopened, destination)

        # A figure has to come from a run that can still be written to, so the
        # analysis re-open is the one that draws it.  ``reopen_for_analysis``
        # is exactly that seam: derived artifacts allowed, recorded data not.
        from ..store.session_store import SessionStore

        analysis = SessionStore.reopen_for_analysis(self.reopened.run_dir)
        samples = list(analysis.read_telemetry())
        metric = next((s.get("metric") for s in samples
                       if s.get("value") is not None), None)
        if metric is None:
            raise ScenarioError("S10-10",
                                "the run carries no measured sample to plot")
        scopes = sorted({(s.get("scope") or {}).get("id") for s in samples
                         if s.get("metric") == metric
                         and (s.get("scope") or {}).get("id")})
        series = [series_from_samples(samples, metric=metric, scope_id=scope,
                                      run_id=analysis.run_id)
                  for scope in scopes] or [
            series_from_samples(samples, metric=metric, run_id=analysis.run_id)]
        events = list(analysis.read_events())

        def x_of(event: Mapping[str, Any]) -> Optional[float]:
            value = event.get("tRelS")
            return float(value) if isinstance(value, (int, float)) else None

        unit = next((s.get("unit") for s in samples
                     if s.get("metric") == metric and s.get("unit")), "")
        spec = FigureSpec(
            figure_id="s10-kpi",
            title=f"{metric} - section 10 replay",
            x_label="sample", x_unit="index",
            y_label=metric, y_unit=str(unit or ""),
            caption="Section 10 ten-step flow, driven over a recorded source.",
            annotations=annotations_from_events(events, x_of=x_of))
        figure = export_figure(analysis, spec, series)
        self._figures.append(figure)
        result = analysis.finalize_analysis()

        written = set(manifest.get("files") or ())
        ok = (bool(written & {"telemetry.csv", "telemetry.json"})
              and Path(destination, "EXPORT-MANIFEST.json").is_file()
              and all(Path(analysis.run_dir, p).is_file()
                      for p in figure["paths"])
              and manifest.get("mode") == "REPLAY")
        return self._record(10, ok, {
            "exportDir": str(destination),
            "exportedFiles": sorted(written),
            "exportMode": manifest.get("mode"),
            "exportSourceMode": manifest.get("sourceMode"),
            "metricAvailability": manifest.get("metricAvailability"),
            "figureId": figure["figureId"],
            "figurePaths": figure["paths"],
            "figureSourceCsv": figure["sourceCsv"],
            "figureWatermarked": figure["mode"] != "LIVE",
            "analysisFinalized": dict(result) if isinstance(result, Mapping)
            else None,
        }, "" if ok else "the export did not produce its data and figure set")

    # -- run ---------------------------------------------------------------- #

    def run(self) -> ScenarioResult:
        """Carry out all ten steps.  Raises :class:`ScenarioError` on the first
        step that cannot be completed, having recorded every step before it."""
        for method in (self.step_01_load_profile, self.step_02_preflight,
                       self.step_03_start_session, self.step_04_submit_intent,
                       self.step_05_decision_path,
                       self.step_06_policy_and_evidence,
                       self.step_07_kpi_and_annotations,
                       self.step_08_finalize, self.step_09_reopen,
                       self.step_10_export):
            method()
        return self.result()

    def result(self) -> ScenarioResult:
        return ScenarioResult(
            steps=tuple(self._steps),
            source_run_dir=(str(self.source_store.run_dir)
                            if self.source_store is not None else None),
            session_run_dir=(str(self.session_store.run_dir)
                             if self.session_store is not None else None),
            session_run_id=self.controller.run_id,
            mode=self.controller.state().mode,
            source_mode=(self.console.replay_source or {}).get("sourceMode"),
            export_dir=(self._steps[-1].evidence.get("exportDir")
                        if self._steps and self._steps[-1].number == 10
                        else None),
            figures=tuple(self._figures))


def run_ten_step_replay(source_path: Any, *, runs_root: Any,
                        profile_path: Optional[Any] = None,
                        capability_manifest: Optional[Mapping[str, Any]] = None,
                        intent_text: Optional[str] = None,
                        session_id: Optional[str] = None,
                        export_dir: Optional[Any] = None,
                        console: Optional[Any] = None,
                        on_step: Optional[Callable[[StepResult], None]] = None
                        ) -> Tuple[Any, ScenarioResult]:
    """Build a console if none was given, and drive the ten steps over it.

    Returns ``(console, result)`` so a caller with a display can keep driving
    the same console afterwards - the Demo View screenshot, for instance.
    """
    if console is None:
        from ..app import OperatorConsole

        console = OperatorConsole(runs_root=str(runs_root),
                                  capability_manifest=capability_manifest)
    kwargs: Dict[str, Any] = {}
    if intent_text is not None:
        kwargs["intent_text"] = intent_text
    scenario = TenStepReplayScenario(
        console, source_path=source_path, profile_path=profile_path,
        session_id=session_id, export_dir=export_dir, on_step=on_step, **kwargs)
    return console, scenario.run()


# --------------------------------------------------------------------------- #
# The Live composition flow
# --------------------------------------------------------------------------- #

#: The ten steps of the Live composition, in order.  They are the Replay ten
#: steps' sibling and deliberately not a copy: a Live session's first step is
#: proving the console contacted *nothing*, and its fifth is proving the
#: preserved coordinator was entered exactly once.
LIVE_STEPS: Tuple[Tuple[str, str], ...] = (
    ("L10-01", "Open Disconnected, having contacted nothing"),
    ("L10-02", "Select a Live integration profile and bind its deployment"),
    ("L10-03", "Pass R1/capability preflight, choose Live, start the session"),
    ("L10-04", "Preview and submit a natural-language intent"),
    ("L10-05", "The preserved three-stage coordinator ran exactly once"),
    ("L10-06", "An R1 outbound was produced and satisfies the contract"),
    ("L10-07", "Status is projected and the active intent is updated"),
    ("L10-08", "S0-S6 and the Eq.12 disposition are displayed"),
    ("L10-09", "Export the data, the metadata and any figure the run earned"),
    ("L10-10", "Finalize the session and leave no residual console state"),
)


class TenStepLiveScenario:
    """Drive the ten Live-composition steps over one bound deployment.

    Like its Replay sibling it drives the *real* console through
    ``handle_action`` and the controller, including the confirmation gate and
    the worker threads.  What it adds is an independent count of coordinator
    entries: it wraps ``run_gui_once`` rather than replacing it, so the episode
    that runs is the authoritative one and the count is a fact about it.
    """

    def __init__(self, console: Any, *, profile_path: Any,
                 intent_text: str,
                 runtime_values: Optional[Mapping[str, Any]] = None,
                 insecure_dev: bool = False,
                 state_dir: Optional[Any] = None,
                 llm_manager: Any = None,
                 export_dir: Optional[Any] = None,
                 on_step: Optional[Callable[[StepResult], None]] = None,
                 worker_timeout: float = 120.0) -> None:
        self.console = console
        self.controller = console.controller
        self.profile_path = Path(profile_path)
        self.intent_text = intent_text
        self.runtime_values = dict(runtime_values or {})
        self.insecure_dev = bool(insecure_dev)
        self.state_dir = Path(state_dir) if state_dir else None
        self.llm_manager = llm_manager
        self.export_dir = Path(export_dir) if export_dir else None
        self.on_step = on_step
        self.worker_timeout = worker_timeout

        self.integration: Any = None
        self.episode: Any = None
        self.export: Mapping[str, Any] = {}
        self._entries: List[Mapping[str, Any]] = []
        self._steps: List[StepResult] = []
        #: The episode entry this run actually wrapped, named in the evidence
        #: so a reader can see *which* runtime the entries were counted on.
        self._entry_module: Optional[str] = None

    # -- plumbing ----------------------------------------------------------- #

    def _settle(self) -> None:
        self.controller.join_workers(timeout=self.worker_timeout)
        try:
            self.console.bus.drain(budget=100000)
        except Exception:                                 # pragma: no cover
            logger.debug("bus drain during live scenario", exc_info=True)

    def _record(self, number: int, ok: bool, evidence: Mapping[str, Any],
                detail: str = "") -> StepResult:
        step_id, title = LIVE_STEPS[number - 1]
        result = StepResult(number=number, step_id=step_id, title=title, ok=ok,
                            evidence=dict(evidence), detail=detail)
        self._steps.append(result)
        if self.on_step is not None:
            try:
                self.on_step(result)
            except Exception:                             # pragma: no cover
                logger.exception("live scenario on_step hook failed")
        if not ok:
            raise ScenarioError(step_id, detail or "step did not complete")
        return result

    def _counting_runner(self) -> Callable[..., Mapping[str, Any]]:
        """Wrap the console's *own* episode entry so entries can be counted.

        The entry is taken from the console's legacy episode port rather than
        imported: this driver replays the preserved Coordinator campaign, and a
        console that was not handed that runtime has no episode for it to count
        - which is refused here by name instead of this module going and
        importing one behind the console's back.
        """
        from .composition import NO_LEGACY_EPISODE

        support = getattr(self.console, "legacy_episode", None)
        if support is None:
            raise ScenarioError("L10-04", NO_LEGACY_EPISODE)
        entry = support.episode_runner()
        self._entry_module = (f"{getattr(entry, '__module__', '?')}."
                              f"{getattr(entry, '__qualname__', '?')}")

        def runner(**kwargs: Any) -> Mapping[str, Any]:
            self._entries.append({
                "integrationPath": kwargs.get("integration_path"),
                "intentText": (kwargs.get("request") or {}).get("intentText"),
                "at": _utc_now(),
            })
            return entry(**kwargs)

        return runner

    def _store(self) -> Any:
        store = getattr(self.controller, "_store", None)
        if store is None:
            raise ScenarioError("L10-03", "the session has no store")
        return store

    # -- the ten steps ------------------------------------------------------- #

    def step_01_disconnected(self) -> StepResult:
        state = self.controller.state()
        ok = (state.mode == "DISCONNECTED" and not state.is_live
              and self.console.live is None
              and self.console.replay_source is None
              and self.console.requested_mode is None
              and not self.controller.preflight_results)
        return self._record(1, ok, {
            "mode": state.mode,
            "isLive": state.is_live,
            "hasIntegration": self.console.live is not None,
            "hasRecordedSource": bool(self.console.replay_source),
            "requestedMode": self.console.requested_mode,
            "preflightChecks": len(self.controller.preflight_results),
            "meaning": "the console has opened no deployment and no recording; "
                       "it observes nothing and claims nothing",
        }, "" if ok else f"the console did not open Disconnected ({state.mode})")

    def step_02_select_profile(self) -> StepResult:
        self.console.handle_action("profile_load", str(self.profile_path))
        self._settle()
        thread = self.controller.run_in_worker(
            "attach-live-integration",
            lambda: setattr(self, "integration",
                            self.console.attach_live_from_profile(
                                state_dir=self.state_dir,
                                runtime_values=self.runtime_values or None,
                                insecure_dev=self.insecure_dev,
                                llm_manager=self.llm_manager)))
        thread.join(timeout=self.worker_timeout)
        self._settle()
        composition = self.console.live
        if composition is None:
            raise ScenarioError(
                "L10-02", "the profile's deployment was not bound; see the "
                          "timeline for the refusal")
        composition.episode_runner = self._counting_runner()
        identity = composition.identity()
        profile = self.console.profile
        ok = (bool(profile.integration_values_path)
              and bool(identity.get("capabilityManifestId"))
              and bool(identity.get("r1ApiRoot"))
              # Binding is not connecting: the mode must still be Disconnected.
              and self.controller.state().mode == "DISCONNECTED")
        return self._record(2, ok, {
            "profileId": profile.profile_id,
            "integrationValuesPath": profile.integration_values_path,
            "integration": identity,
            "modeAfterBinding": self.controller.state().mode,
            "meaning": "the console knows which deployment it would address; "
                       "nothing has been contacted yet",
        }, "" if ok else "the deployment binding is incomplete")

    def step_03_preflight_and_start(self) -> StepResult:
        self.console.handle_action("preflight")
        self._settle()
        checks = {view.check_id: view for view in
                  self.controller.preflight_results}
        bootstrap = checks.get("PF-R1-BOOTSTRAP")
        capability = checks.get("PF-CAPABILITY")
        transport_ok = bootstrap is not None and bootstrap.status == "OK"
        if not transport_ok:
            self._record(3, False, {
                "checks": [{"id": v.check_id, "status": v.status,
                            "reason": v.reason, "detail": v.detail}
                           for v in self.controller.preflight_results]},
                "R1 preflight did not reach the deployment: "
                + str((bootstrap.reason if bootstrap else None)
                      or "no PF-R1-BOOTSTRAP result"))
        self.console.select_mode("LIVE")
        mode, evidence = self.console.session_mode_evidence()
        self.console.handle_action("start")
        self._settle()
        state = self.controller.state()
        ok = (state.mode == "LIVE" and state.is_live
              and state.disposition == "RUNNING"
              and bool(self.controller.run_id))
        return self._record(3, ok, {
            "checks": [{"id": v.check_id, "status": v.status,
                        "boundary": v.boundary, "reason": v.reason,
                        "detail": v.detail}
                       for v in self.controller.preflight_results],
            "capabilityCheck": getattr(capability, "status", None),
            "mode": state.mode,
            "modeEvidence": dict(evidence),
            "runId": self.controller.run_id,
            "runDir": str(self._store().run_dir),
            "recording": state.recording,
            "startMeaning": "readiness verified and the intent/policy/evidence "
                            "workflow enabled; no network element was started",
        }, "" if ok else f"the session did not start LIVE ({state.mode}, "
                         f"{state.disposition})")

    def step_04_preview_and_submit(self) -> StepResult:
        previous_confirm = self.console.confirm
        seen: List[Any] = []

        def auto_confirm(spec):
            seen.append(spec)
            return evaluate_confirmation(spec, acknowledged=True,
                                         typed=spec.typed_phrase)

        self.console.confirm = auto_confirm
        submitted: List[Any] = []
        original = self.console.intent_submitter

        def submitter(text: str) -> Any:
            episode = original(text)
            submitted.append(episode)
            return episode

        self.console.intent_submitter = submitter
        try:
            self.console.handle_action("preview", self.intent_text)
            self._settle()
            self.console.handle_action("submit", self.intent_text)
            self._settle()
        finally:
            self.console.confirm = previous_confirm
            self.console.intent_submitter = original
        self.episode = submitted[0] if submitted else None
        kinds = [event.kind for event in self.controller.timeline]
        preview = next((e for e in self.controller.timeline
                        if e.kind == "INTENT_PREVIEWED"), None)
        ok = ("INTENT_SUBMITTED" in kinds and bool(seen)
              and self.episode is not None)
        return self._record(4, ok, {
            "intentText": self.intent_text,
            "previewShown": preview is not None,
            "previewSubmittable": (preview.detail if preview else {})
            .get("submittable"),
            "previewIntegration": dict((preview.detail if preview else {})
                                       .get("integration") or {}),
            "previewReason": (preview.detail if preview else {}).get("reason"),
            "confirmationShown": bool(seen),
            "confirmationTargets": list(seen[0].targets) if seen else [],
            "confirmationEffects": list(seen[0].effects) if seen else [],
            "contractIntentId": getattr(self.episode, "intent_id", None),
        }, "" if ok else "the submission did not produce an episode")

    def step_05_one_coordinator_run(self) -> StepResult:
        composition = self.console.live
        episodes = list(self._store().read_episodes()) if self._store() else []
        ok = (len(self._entries) == 1 and composition.submissions == 1
              and composition.in_flight is None and len(episodes) == 1)
        # A second submission of the same text, while nothing is in flight, is
        # a *new* operator action; what must never happen is one action
        # producing two entries.  The duplicate-click guard is asserted by the
        # test that drives two clicks, not by this count.
        return self._record(5, ok, {
            "coordinatorEntries": len(self._entries),
            "entries": [dict(entry) for entry in self._entries],
            "submissions": composition.submissions,
            "inFlight": composition.in_flight,
            "storedEpisodes": len(episodes),
            "proposerAtSubmit": getattr(self.episode, "proposer_at_submit", None),
            "entryModule": self._entry_module,
            "meaning": "the GUI has no coordinator of its own; it entered the "
                       "same authority the headless rApp enters",
        }, "" if ok else f"{len(self._entries)} coordinator entries for one "
                         "submission")

    def step_06_r1_outbound(self) -> StepResult:
        outbound = self.episode.r1_outbound()
        ok = bool(outbound.get("policyId")) and outbound.get("contractValid") is True
        return self._record(6, ok, {
            "r1Outbound": dict(outbound),
            "policyStatus": dict(self.episode.policy_status()),
            "validatedAgainst": "AIC_UECellSteering_1.0.0.policy",
            "meaning": "the object the deployment received was re-validated "
                       "against the frozen contract schema, not assumed valid "
                       "because the request was accepted",
        }, "" if ok else f"no contract-valid R1 outbound: "
                         f"{outbound.get('contractReason')}")

    def step_07_status_and_active_intent(self) -> StepResult:
        state = self.controller.state()
        rows = list(state.intents)
        projected = next((e for e in reversed(self.controller.timeline)
                          if e.kind == "STATUS_PROJECTED"), None)
        ok = bool(rows) and bool(state.components) and projected is not None
        return self._record(7, ok, {
            "activeIntents": [{"intentId": row.intent_id,
                               "lifecycle": row.lifecycle,
                               "eq12State": row.eq12_state,
                               "policyId": row.policy_id,
                               "policyStatus": row.policy_status,
                               "evidenceStatus": row.evidence_status}
                              for row in rows],
            "components": len(state.components),
            "componentStatuses": sorted({c.status for c in state.components}),
            "statusProjection": dict(projected.detail) if projected else {},
            "unsupportedElements": [c.element_id for c in state.components
                                    if c.status in ("UNSUPPORTED", "UNKNOWN")],
        }, "" if ok else "no active intent row or no projected status")

    def step_08_fsm_and_disposition(self) -> StepResult:
        decision = self.episode.decision
        fsm = [(s.stage_id, s.state) for s in decision.fsm_stages]
        ok = bool(fsm) and decision.eq12_state in (
            "Admitted", "NotAdmitted", "TechnicalFailsafe")
        return self._record(8, ok, {
            "episodeId": decision.episode_id,
            "fsmStages": [{"id": sid, "state": state} for sid, state in fsm],
            "fsmVisited": [sid for sid, state in fsm if state != "PENDING"],
            "llmStages": [{"id": s.stage_id, "state": s.state,
                           "durationMs": s.duration_ms}
                          for s in decision.llm_stages],
            "terminalOutcome": decision.terminal_outcome,
            "terminalReason": decision.terminal_reason,
            "eq12State": decision.eq12_state,
            "rawConfidence": decision.raw_confidence,
            "calibratedProbability": decision.calibrated_probability,
            "thetaStar": decision.theta_star,
            "rolledBack": decision.rolled_back,
        }, "" if ok else "the episode produced no Eq.12 terminal state")

    def step_09_export(self) -> StepResult:
        result = self.console.export_current_run(
            figure_id="live-composition", destination=self.export_dir)
        self.export = dict(result)
        context = dict(result.get("operatorContext") or {})
        files = sorted(result.get("files") or ())
        integration = dict(context.get("integration") or {})
        ok = (bool({"decision.json", "summary.json"} & set(files))
              and context.get("mode") == "LIVE"
              and bool(integration.get("capabilityManifestId")))
        return self._record(9, ok, {
            "exportDir": result.get("exportDir"),
            "exportedFiles": files,
            "exportMode": context.get("mode"),
            "integration": integration,
            "activeBackend": context.get("activeBackend"),
            "exportedAt": context.get("exportedAt"),
            "figureNote": "a Live session with no telemetry source draws no "
                          "figure; the run's own metric availability says why, "
                          "and no plot is produced from data that was never "
                          "measured [GAP-05]",
        }, "" if ok else "the export did not carry its data and identity")

    def step_10_finalize(self) -> StepResult:
        run_dir = str(self._store().run_dir)
        self.console.handle_action("stop")
        self._settle()
        disposition = self.controller.disposition
        self.console.detach(reason="live composition flow completed")
        self._settle()
        composition_clear = (self.console.live is None
                             and self.console.replay_source is None
                             and self.console.requested_mode is None
                             and self.console.intent_submitter is None)
        ok = (disposition == "COMPLETED"
              and Path(run_dir, "manifest.json").is_file()
              and composition_clear
              and self.controller.state().mode == "DISCONNECTED")
        return self._record(10, ok, {
            "runDir": run_dir,
            "disposition": disposition,
            "manifestWritten": Path(run_dir, "manifest.json").is_file(),
            "integrationDetached": self.console.live is None,
            "recordedSourceDetached": self.console.replay_source is None,
            "requestedMode": self.console.requested_mode,
            "submitterCleared": self.console.intent_submitter is None,
            "modeAfterClose": self.controller.state().mode,
        }, "" if ok else f"the run finalized as {disposition} or state remained")

    # -- run ----------------------------------------------------------------- #

    def run(self) -> ScenarioResult:
        for method in (self.step_01_disconnected, self.step_02_select_profile,
                       self.step_03_preflight_and_start,
                       self.step_04_preview_and_submit,
                       self.step_05_one_coordinator_run,
                       self.step_06_r1_outbound,
                       self.step_07_status_and_active_intent,
                       self.step_08_fsm_and_disposition,
                       self.step_09_export, self.step_10_finalize):
            method()
        return self.result()

    def result(self) -> ScenarioResult:
        return ScenarioResult(
            steps=tuple(self._steps), source_run_dir=None,
            session_run_dir=(self._steps[9].evidence.get("runDir")
                             if len(self._steps) >= 10 else None),
            session_run_id=self.controller.run_id,
            mode=(self._steps[2].evidence.get("mode")
                  if len(self._steps) >= 3 else None),
            source_mode=None,
            export_dir=(self._steps[8].evidence.get("exportDir")
                        if len(self._steps) >= 9 else None))

    @property
    def ok(self) -> bool:
        return (len(self._steps) == len(LIVE_STEPS)
                and all(step.ok for step in self._steps))


def load_capability_manifest(path: Any) -> Mapping[str, Any]:
    """Read a capability manifest for the console, or raise a clear error."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


__all__ = ["LIVE_STEPS", "STEPS", "ScenarioError", "ScenarioResult",
           "StepResult", "TenStepLiveScenario", "TenStepReplayScenario",
           "load_capability_manifest", "run_ten_step_replay"]
