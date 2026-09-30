"""Replay adapter - offline experiments-runner output.

Owner: track **T3**.  Authority: ``docs/phase-b-gui/replay-sources.1.0.0.json``
(adapter ``experiment-run``).

This is the source that actually carries the rich KPI set - throughput and the
per-direction radio metrics - plus the full ``EpisodeRecord`` decision and
latency decomposition.  Four rules govern how it is read, and each of them
exists because the obvious shortcut would misdescribe the data:

**The session mode is REPLAY; the recording's own mode is separate.**  Reading a
stored runner output back is a Replay session, always - nothing is being
observed.  ``_meta.mode`` is read, never guessed, and becomes ``sourceMode``:
supplementary provenance that says what the recording was, carried in the config
snapshot, the summary, the source entry and every export.  A run whose
``_meta.mode`` is absent records ``sourceMode`` ``SYNTHETIC`` with a data issue,
because the offline default is the safe assumption.

This is the correction for the defect that a recorded ``_meta.mode=live`` run
presented as LIVE: ``manifest.mode`` drives the badge, the export banner and the
figure watermark, so a recording that carries LIVE there is a Replay that reads
as live - the exact confusion phaseB_task.md section 4 and section 9 forbid.
"Live" described where the radio was; it never described this session.

**The source class is EXPERIMENT_RECORD regardless.**  Even for a recording made
against a live radio, the numbers did not arrive over an O-RAN interface, and the
metric panel says so.

**Relative time stays relative.**  ``StepRecord`` carries ``t_s``, elapsed
seconds from the trial start, and no wall clock.  Samples are written with
``tRelS`` set and ``tUtc`` null and the axis is labelled "elapsed (s)".  Adding
``t_s`` to the session-id timestamp would manufacture an absolute clock that was
never observed.

**The legacy radio keys are ambiguous and are not resolved by guessing.**  The
synthetic harness writes ``ue_kpis`` keys ``rsrp`` and ``sinr``, which do not say
whether the value is uplink or downlink.  The metric registry keeps UL and DL as
separate series and forbids merging them, so those values are **not** mapped onto
``ue_dl_ss_rsrp_dbm`` or ``gnb_ul_avg_rsrp_dbm``; the metrics render
``UNAVAILABLE`` with the reason.  The live measurement path writes the honest
per-direction keys, and those are mapped.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Final, List, Mapping, Optional, Tuple

SOURCE_ID: Final[str] = "experiment-run"

#: ``_meta.mode`` -> ``sourceMode``, the mode of the RECORDING.  Never
#: ``manifest.mode``, which is the mode of this session and is always REPLAY
#: here.  The adapter never guesses either one.
MODE_MAP: Final[Dict[str, str]] = {
    "synthetic": "SYNTHETIC",
    "emulated": "EMULATED",
    "live": "LIVE",
}

#: Honest per-direction ``ue_kpis`` keys, mapped onto registry metric ids.
UE_METRICS: Final[Dict[str, Tuple[str, str]]] = {
    "throughput_mbps": ("throughput_mbps", "Mbps"),
    "gnb_ul_avg_rsrp_dbm": ("gnb_ul_avg_rsrp_dbm", "dBm"),
    "gnb_ul_snr_db": ("gnb_ul_snr_db", "dB"),
    "ue_dl_ss_rsrp_dbm": ("ue_dl_ss_rsrp_dbm", "dBm"),
    "ue_dl_sinr_db": ("ue_dl_sinr_db", "dB"),
}

#: Legacy ambiguous keys.  Present in synthetic output; deliberately unmapped.
AMBIGUOUS_KEYS: Final[Tuple[str, ...]] = ("rsrp", "sinr")

AMBIGUOUS_REASON: Final[str] = (
    "the run records the legacy ambiguous ue_kpis key {key!r}, which does not "
    "state whether the value is uplink or downlink; UL and DL are separate "
    "series and are never merged")

#: ``EpisodeRecord`` latency fields -> the CycleTrace latency block.
_LATENCY_FIELDS: Final[Dict[str, str]] = {
    "inference_ms": "inferenceMs",
    "schema_admission_ms": "schemaAdmissionMs",
    "executor_write_ms": "executorWriteMs",
    "readback_ms": "readbackMs",
    "validation_ms": "validationMs",
    "negotiation_ms": "negotiationMs",
    "rollback_ms": "rollbackMs",
    "recovery_ms": "recoveryMs",
}


class ExperimentRunError(Exception):
    """An experiment_results directory that cannot be loaded."""

    def __init__(self, detail: str, *, kind: str = "SCHEMA_MISMATCH") -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def list_sessions(results_dir) -> List[str]:
    """Session ids present in a flat ``experiment_results`` directory.

    The runner writes one flat directory discriminated by ``session_id`` rather
    than one directory per run, so discovery is by filename, not by listing
    subdirectories.
    """
    root = Path(results_dir)
    if not root.is_dir():
        return []
    sessions = set()
    for path in root.glob("*_metrics.json"):
        stem = path.name[: -len("_metrics.json")]
        if "_" in stem:
            sessions.add(stem.split("_", 1)[1])
    return sorted(sessions)


class ExperimentRunSource:
    """One runner session: its steps, episodes and metrics documents."""

    def __init__(self, results_dir, session_id: str) -> None:
        self.root = Path(results_dir)
        self.session_id = session_id
        if not self.root.is_dir():
            raise ExperimentRunError(
                f"experiment results directory not found: {self.root}",
                kind="CONNECTION_LOST")
        self.metrics_path = self._one(f"*_{session_id}_metrics.json")
        self.label = self.metrics_path.name[
            : -len(f"_{session_id}_metrics.json")]
        self.metrics = self._read(self.metrics_path)
        self.steps = self._read_optional(
            self.root / f"{self.label}_{session_id}_steps.json", [])
        self.episodes = self._read_optional(
            self.root / f"{self.label}_{session_id}_episodes.json", [])
        self.issues: List[Tuple[str, str]] = []

        meta = self.metrics.get("_meta") or {}
        declared = meta.get("mode")
        if declared is None:
            self.source_mode = "SYNTHETIC"
            self.issues.append((
                "PARTIAL_DATA",
                "_metrics.json carries no _meta.mode; the recording is treated "
                "as SYNTHETIC because the offline default is the safe "
                "assumption, and the mode is never inferred from the values"))
        elif str(declared) not in MODE_MAP:
            raise ExperimentRunError(
                f"_meta.mode {declared!r} is not one of "
                + ", ".join(sorted(MODE_MAP)))
        else:
            self.source_mode = MODE_MAP[str(declared)]

        #: The mode of THIS session.  Reading a recording back is a Replay,
        #: whatever the recording was made against.
        self.mode = "REPLAY"

    def _one(self, pattern: str) -> Path:
        found = sorted(self.root.glob(pattern))
        if not found:
            raise ExperimentRunError(
                f"no {pattern} under {self.root.name}", kind="CONNECTION_LOST")
        if len(found) > 1:
            raise ExperimentRunError(
                "more than one metrics document for this session: "
                + ", ".join(p.name for p in found))
        return found[0]

    @staticmethod
    def _read(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ExperimentRunError(f"{path.name} is not valid JSON: {exc}",
                                     kind="DATA_TRUNCATED") from exc

    @classmethod
    def _read_optional(cls, path: Path, default: Any) -> Any:
        return cls._read(path) if path.is_file() else default

    @property
    def meta(self) -> Mapping[str, Any]:
        return self.metrics.get("_meta") or {}

    @property
    def config(self) -> Mapping[str, Any]:
        return self.meta.get("config") or {}

    @property
    def provenance_note(self) -> Optional[str]:
        """A free-text provenance note the producer attached to the run.

        Used to carry facts about the data that the numbers cannot carry
        themselves - for instance that a committed fixture's values are
        illustrative rather than measured.  It travels into the summary and into
        every export, because a reader who receives only the export must still
        learn it.
        """
        note = self.meta.get("provenance_note")
        return str(note) if note else None

    @property
    def goal_mbps(self) -> Optional[float]:
        """The I2 goal, read from this run's own snapshot.

        The offline default is 8.0 and the live target is 3.5; reading either
        from a module constant would mislabel half the runs, so it comes from
        the run.
        """
        intent = self.config.get("intent_config") or {}
        value = intent.get("throughput_target_mbps")
        return float(value) if isinstance(value, (int, float)) else None


def build_telemetry(source: ExperimentRunSource
                    ) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
    """One sample per UE per metric per step, plus the issues that arose."""
    from gui.operator.store.records import telemetry_sample

    samples: List[Dict[str, Any]] = []
    issues: List[Tuple[str, str]] = []
    ambiguous_seen = set()
    seq = 0
    for step in source.steps:
        t_rel = step.get("t_s")
        for ue_id, kpis in (step.get("ue_kpis") or {}).items():
            for key, value in (kpis or {}).items():
                if key in AMBIGUOUS_KEYS:
                    ambiguous_seen.add(key)
                    continue
                if key not in UE_METRICS:
                    continue
                metric, unit = UE_METRICS[key]
                samples.append(telemetry_sample(
                    seq=seq, metric=metric,
                    value=float(value) if isinstance(value, (int, float))
                    and not isinstance(value, bool) else None,
                    unit=unit, scope_level="UE", scope_id=str(ue_id),
                    boundary="EXPERIMENT_RECORD",
                    management_service="research measurement path",
                    t_rel_s=float(t_rel) if t_rel is not None else None,
                    # Stated, not defaulted: the research measurement path
                    # writes a value only when its probe succeeded, and writes
                    # null when it did not, so a present value is vouched for by
                    # the recorder and an absent one is MISSING.
                    quality="OK" if value is not None else "MISSING"))
                seq += 1

    for key in sorted(ambiguous_seen):
        issues.append(("UNSUPPORTED_FIELD", AMBIGUOUS_REASON.format(key=key)))
    return samples, issues


def build_decision(source: ExperimentRunSource
                   ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]],
                              List[Dict[str, Any]]]:
    """Episodes, cycles and LLM calls from ``EpisodeRecord[]``.

    ``EpisodeRecord`` is per *cycle*: one record per autonomous coordination
    cycle within an episode, which is the statistical unit the engine calibrates
    on.  Records sharing an episode id are folded into one EpisodeTrace with one
    CycleTrace each, so the decision view can show a negotiation re-entry as a
    second cycle of the same episode rather than as a second episode.
    """
    from gui.operator.store.records import cycle_trace, episode_trace, llm_call_trace

    episodes: Dict[str, Dict[str, Any]] = {}
    cycles: List[Dict[str, Any]] = []
    llm_calls: List[Dict[str, Any]] = []

    for index, record in enumerate(source.episodes):
        episode_id = (record.get("evidence_episode_id")
                      or f"{source.session_id}-{record.get('method')}"
                         f"-t{record.get('trial_id')}-{record.get('phase_idx')}")
        outcome = record.get("terminal_outcome")
        if episode_id not in episodes:
            episodes[episode_id] = episode_trace(
                episode_id=episode_id,
                intent_text=str(record.get("method", "")),
                terminal_outcome=outcome,
                terminalReason=record.get("terminal_reason"),
                experimentRunId=record.get("evidence_experiment_run_id"),
                evidenceRecordId=record.get("evidence_cycle_id"),
                fsmPath=[],
                success=bool(record.get("success")),
                latencyMs=record.get("total_latency_ms"),
                parseMs=record.get("parse_ms"),
                schemaValid=record.get("schema_valid"),
                negotiationRounds=record.get("negotiation_rounds"),
                rolledBack=record.get("rolled_back"),
                policyIds=[],
                trialId=record.get("trial_id"),
                method=record.get("method"),
                phase=record.get("phase"),
                phaseIdx=record.get("phase_idx"))
        else:
            trace = episodes[episode_id]
            if outcome is not None and trace["terminalOutcome"] is None:
                merged = episode_trace(episode_id=episode_id,
                                       intent_text=trace["intentText"],
                                       terminal_outcome=outcome)
                trace["terminalOutcome"] = merged["terminalOutcome"]
                trace["eq12State"] = merged["eq12State"]
            trace["success"] = bool(trace.get("success")) or bool(record.get("success"))

        cycles.append(cycle_trace(
            episode_id=episode_id,
            cycle_index=int(record.get("cycle_idx") or 0),
            cycleMonotonicS=record.get("episode_monotonic_s"),
            feasible=record.get("predicted_feasible"),
            rawConfidence=record.get("raw_confidence"),
            calibratedProbability=record.get("calibrated_probability"),
            threshold=record.get("threshold"),
            thresholdAppliedTo=record.get("threshold_applied_to")
            or "calibrated_probability",
            proposalEligible=record.get("proposal_eligible"),
            routedTo=record.get("routed_to"),
            negoStats={"rounds": record.get("negotiation_rounds"),
                       "entered": record.get("entered_negotiation"),
                       "durationS": record.get("nego_duration")},
            agreement=None,
            alternatives=[],
            latencies={target: record.get(field)
                       for field, target in _LATENCY_FIELDS.items()},
            clips=list(record.get("clips") or ()),
            thetaStar=record.get("theta_star")))

        if record.get("inference_ms") is not None or record.get("model_id"):
            llm_calls.append(llm_call_trace(
                seq=index, stage="FEASIBILITY",
                backend_name=str(record.get("model_id") or "unknown"),
                episodeId=episode_id,
                cycleId=record.get("evidence_cycle_id"),
                modelVersion=record.get("evidence_model_version"),
                promptHash=record.get("evidence_prompt_hash"),
                latencyMs=record.get("inference_ms"),
                schemaValid=record.get("schema_valid"),
                success=record.get("proposal_generated")))
        if record.get("parse_ms") is not None:
            llm_calls.append(llm_call_trace(
                seq=len(llm_calls), stage="PARSE",
                backend_name=str(record.get("model_id") or "unknown"),
                episodeId=episode_id, latencyMs=record.get("parse_ms")))
        if record.get("negotiation_ms") is not None:
            llm_calls.append(llm_call_trace(
                seq=len(llm_calls), stage="ALTERNATIVES",
                backend_name=str(record.get("model_id") or "unknown"),
                episodeId=episode_id, latencyMs=record.get("negotiation_ms")))

    return list(episodes.values()), cycles, llm_calls


def build_timeline(source: ExperimentRunSource) -> List[Dict[str, Any]]:
    """Phase boundaries and per-episode decision events.

    An event with a real epoch field gets ``tUtc``; everything else carries
    ``tRelS`` and ``origin=DERIVED``, because relative ordering is all the
    record actually supports.
    """
    from datetime import datetime, timezone

    from gui.operator.store.records import timeline_event

    def utc(epoch: Any) -> Optional[str]:
        if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
            return None
        return datetime.fromtimestamp(float(epoch), timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    rows: List[Dict[str, Any]] = []
    seq = 0
    seen_phase = None
    for step in source.steps:
        phase_key = step.get("phase_label") or step.get("phase")
        if phase_key != seen_phase:
            seen_phase = phase_key
            rows.append(timeline_event(
                seq=seq, lane="SESSION", kind="PHASE_CHANGE", origin="DERIVED",
                derivation="phase boundary inferred from the step sequence; "
                           "StepRecord carries elapsed time only",
                t_rel_s=step.get("t_s"), title=f"phase {phase_key}",
                detail={"phase": step.get("phase"),
                        "phaseIdx": step.get("phase_idx"),
                        "phaseLabel": step.get("phase_label")}))
            seq += 1

    for record in source.episodes:
        episode_id = (record.get("evidence_episode_id")
                      or f"{source.session_id}-{record.get('method')}"
                         f"-t{record.get('trial_id')}-{record.get('phase_idx')}")
        ids = {"episodeId": episode_id,
               "cycleId": record.get("evidence_cycle_id")}
        rows.append(timeline_event(
            seq=seq, lane="DECISION", kind="DECISION_COMPLETE",
            origin="DERIVED",
            derivation="the episode record carries a monotonic offset, not a "
                       "wall clock",
            t_rel_s=record.get("episode_monotonic_s"),
            title=f"routed to {record.get('routed_to')}",
            detail={"routedTo": record.get("routed_to"),
                    "calibratedProbability": record.get("calibrated_probability"),
                    "threshold": record.get("threshold")},
            ids=ids))
        seq += 1
        if record.get("action_apply_time") is not None:
            rows.append(timeline_event(
                seq=seq, lane="COORDINATOR", kind="ACTION_APPLIED",
                t_utc=utc(record.get("action_apply_time")),
                title="action applied", ids=ids,
                detail={"appliedAction": record.get("applied_action")}))
            seq += 1
        if record.get("final_readback_time") is not None:
            rows.append(timeline_event(
                seq=seq, lane="COORDINATOR", kind="READBACK",
                t_utc=utc(record.get("final_readback_time")),
                title="readback", ids=ids,
                detail={"readbackAction": record.get("readback_action")}))
            seq += 1
        if record.get("entered_negotiation"):
            rows.append(timeline_event(
                seq=seq, lane="DECISION", kind="NEGOTIATION_ROUND",
                origin="DERIVED",
                derivation="negotiation entry is recorded as a flag, not a time",
                t_rel_s=record.get("episode_monotonic_s"),
                title=f"{record.get('negotiation_rounds')} round(s)", ids=ids,
                detail={"rounds": record.get("negotiation_rounds")}))
            seq += 1
        if record.get("rolled_back"):
            rows.append(timeline_event(
                seq=seq, lane="COORDINATOR", kind="ROLLBACK", origin="DERIVED",
                derivation="rollback is recorded as a flag, not a time",
                t_rel_s=record.get("episode_monotonic_s"),
                severity="WARNING", title="rolled back", ids=ids,
                detail={"restoreVerified": record.get("restore_verified")}))
            seq += 1
    return rows


def build_statistics(samples: List[Mapping[str, Any]]) -> Dict[str, Any]:
    """Mean, standard deviation, n and CI per metric per scope.

    The statistics come from ``experiments/metrics.py`` rather than a private
    re-implementation, so an on-screen mean and confidence interval equal the
    paper pipeline's for the same inputs.  ``metrics.py`` stays stdlib-only; the
    GUI calls it and never the reverse.
    """
    from experiments.metrics import confidence_interval

    grouped: Dict[Tuple[str, str], List[float]] = {}
    for sample in samples:
        value = sample.get("value")
        if value is None:
            continue                     # a null is excluded, never imputed
        key = (sample["metric"], sample["scope"]["id"])
        grouped.setdefault(key, []).append(float(value))

    statistics: Dict[str, Any] = {}
    for (metric, scope_id), values in sorted(grouped.items()):
        interval = confidence_interval(values)
        statistics.setdefault(metric, {})[scope_id] = {
            "n": interval["n"],
            "mean": interval["mean"],
            "std": interval["std"],
            "ci95": {"low": interval["low"], "high": interval["high"],
                     "halfWidth": interval["half_width"], "level": 0.95},
            "statistic": "mean with 95% t-CI (experiments.metrics)",
            "excludedNullSamples": sum(
                1 for s in samples
                if s["metric"] == metric and s["scope"]["id"] == scope_id
                and s.get("value") is None),
        }
    return statistics


def load_experiment_run(results_dir, session_id: str, runs_root, *,
                        capability_manifest: Optional[Mapping[str, Any]] = None
                        ) -> Any:
    """Normalize one runner session into a finalized SessionStore run."""
    from gui.operator.store.metric_index import MetricIndexBuilder
    from gui.operator.store.session_store import SessionStore

    source = ExperimentRunSource(results_dir, session_id)
    # Always a Replay session: even a run whose _meta.mode is live is being read
    # back from stored output, not observed over a transport.  sourceMode records
    # what the recording was; manifest.mode records what this session is, and it
    # is the latter that drives the badge, the banner and the watermark.
    store = SessionStore.create(
        runs_root, mode="REPLAY",
        mode_evidence={"basis": "REPLAY_OF_RECORDED_SOURCE",
                       "sourceRunIds": [session_id]},
        profile_id=str(source.meta.get("theta_mode") or "") or None,
        config_snapshot={
            "source": {"adapter": SOURCE_ID, "sessionId": session_id,
                       "label": source.label, "sourceMode": source.source_mode,
                       "provenanceNote": source.provenance_note},
            "meta": source.meta,
            "capabilityManifest": capability_manifest,
        })
    try:
        store.add_source(
            source_id=SOURCE_ID, kind="EXPERIMENT_RUN",
            boundary="LOCAL_PROCESS", path=str(source.root),
            adapter="gui/operator/sources/adapters/experiments_run.py",
            notes=(f"recording of a {source.source_mode} run, replayed; the "
                   "research measurement path did not deliver these values over "
                   "an O-RAN interface"
                   + (f"; {source.provenance_note}" if source.provenance_note
                      else "")))
        for path in (source.metrics_path,
                     source.root / f"{source.label}_{session_id}_steps.json",
                     source.root / f"{source.label}_{session_id}_episodes.json"):
            if path.is_file():
                store.copy_raw(SOURCE_ID, path, path.name)

        for kind, detail in source.issues:
            store.record_issue(kind, detail)

        samples, sample_issues = build_telemetry(source)
        for sample in samples:
            store.append_telemetry(sample)
        for kind, detail in sample_issues:
            store.record_issue(kind, detail, gap_id="GAP-06")

        episodes, cycles, llm_calls = build_decision(source)
        for episode in episodes:
            store.append_episode(episode)
        for cycle in cycles:
            store.append_cycle(cycle)
        for call in llm_calls:
            store.append_llm_call(call)
        for event in build_timeline(source):
            store.append_event(event)

        models = sorted({str(r.get("model_id")) for r in source.episodes
                         if r.get("model_id")})
        if models:
            store.set_llm(backend_name=models[0])

        builder = MetricIndexBuilder(source_id=SOURCE_ID,
                                     capability_manifest=capability_manifest)
        builder.observe_all(samples)
        store.write_metric_index(builder.build())

        outcomes: Dict[str, int] = {}
        terminals: Dict[str, int] = {}
        for episode in episodes:
            if episode["eq12State"]:
                outcomes[episode["eq12State"]] = \
                    outcomes.get(episode["eq12State"], 0) + 1
            if episode["terminalOutcome"]:
                terminals[episode["terminalOutcome"]] = \
                    terminals.get(episode["terminalOutcome"], 0) + 1

        summary = {
            "runId": store.run_id,
            "sourceSessionId": session_id,
            "mode": store.mode,
            "sourceMode": source.source_mode,
            "sourceModeNote":
                "the mode of the RECORDING. This session is "
                f"{store.mode}: nothing here is being observed, and the badge, "
                "the export banner and the figure watermark all follow the "
                "session mode",
            "sourceProvenanceNote": source.provenance_note,
            "sourceClass": "EXPERIMENT_RECORD",
            "sourceClassNote":
                "real measured data that did not arrive over an O-RAN "
                "interface; 'live' describes where the radio was, not which "
                "boundary the numbers came through",
            "outcomes": outcomes,
            "terminalOutcomes": terminals,
            "goal": {"metric": "throughput_mbps", "targetMbps": source.goal_mbps,
                     "source": "config-snapshot.json _meta.config.intent_config"},
            "paperReady": source.meta.get("paper_ready"),
            "paperExportNote": source.meta.get("paper_export"),
            "negotiations": sum(1 for r in source.episodes
                                if r.get("entered_negotiation")),
            "rollbacks": sum(1 for r in source.episodes if r.get("rolled_back")),
            "methodMetrics": {k: v for k, v in source.metrics.items()
                              if k != "_meta"},
            "dataIssues": store.data_issues(),
        }
        statistics = build_statistics(samples)
        store.write_summary(summary, statistics or None)
        store.finalize("COMPLETED")
        return store
    except Exception:
        store.finalize("FAILED")
        raise


__all__ = [
    "AMBIGUOUS_KEYS", "AMBIGUOUS_REASON", "ExperimentRunError",
    "ExperimentRunSource", "MODE_MAP", "SOURCE_ID", "UE_METRICS",
    "build_decision", "build_statistics", "build_telemetry", "build_timeline",
    "list_sessions", "load_experiment_run",
]
