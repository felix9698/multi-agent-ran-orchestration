"""Batch runner which records every case through the common SessionStore schema."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import csv
import hashlib
import json
import os
from pathlib import Path
import platform
import tempfile
from typing import Any, Dict, Mapping, Protocol, Sequence, Tuple

from assurance.batch.metrics import derive_batch_metrics
from assurance.batch.plan import BatchCase, BatchPlan
from assurance.core.confirmation import ConfirmationRecord
from runstore.records import episode_trace, metric_index_entry, telemetry_sample, timeline_event
from runstore.session_store import SessionStore


_PALETTE = ("#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#F0E442")
_FIGURE_DERIVATIONS = {
    "cdf": "empirical_cdf(wallClockS)",
    "box": "boxplot(wallClockS)",
    "violin": "violinplot(wallClockS)",
    "convergence": "closure_progress_by_trial",
    "harm_efficiency": "harmCharge_over_closureEfficiency",
    "stacked_outcome": "outcome_count",
    "timeline": "wallClockS_by_trial",
}


@dataclass(frozen=True)
class CaseExecution:
    """The executor's observation of one Kernel case, not a strategy API."""

    outcome: str
    validity: str
    raw_events: Tuple[Mapping[str, Any], ...]
    measurements: Mapping[str, Any]
    wall_clock_s: float
    recovery_s: float = 0.0
    rolled_back: bool = False
    incident: bool = False
    closure_progress: float = 1.0
    closure_efficiency: float = 1.0
    harm_reserve: float = 0.0
    harm_charge: float = 0.0
    target_debt: float = 0.0
    harm_limit_respected: bool = True
    invariant_violations: int = 0
    proposal_rejects: int = 0
    proposal_stale: int = 0
    fallbacks: int = 0
    agent_tokens: int = 0
    agent_tool_calls: int = 0
    agent_latency_ms: float | None = None
    target_satisfied: bool = False
    hold_satisfied: bool = False
    evidence_reuse: int = 0
    confirmations: int = 0
    post_closure_witnesses: int = 0

    def __post_init__(self) -> None:
        if self.outcome not in {"SUCCESS", "FAIL", "INVALID", "ERROR", "ABORTED"}:
            raise ValueError("unknown case outcome")
        if self.validity not in {"VALID", "INVALID", "ERROR"}:
            raise ValueError("validity must be VALID, INVALID, or ERROR")
        if self.wall_clock_s < 0 or self.recovery_s < 0:
            raise ValueError("timings must be non-negative")
        object.__setattr__(self, "raw_events", tuple(dict(event) for event in self.raw_events))
        object.__setattr__(self, "measurements", dict(self.measurements))


class KernelCaseExecutor(Protocol):
    """Adapter boundary for the existing Kernel route and strategy injection."""

    mode: str

    def execute(self, case: BatchCase, plan: BatchPlan) -> CaseExecution:
        """Run one admitted case through the real Kernel path and return observations."""
        ...


class DeterministicMockKernelExecutor:
    """Hardware-free stand-in for an adapter-backed Kernel executor.

    This does not decide strategy or alter contracts.  It produces an explicit,
    immutable mock event stream in ``REPLAY`` mode so the batch pipeline can be
    exercised without any E2/O1/RAN/UE live call.  A deployment replaces this
    object with a ``KernelCaseExecutor`` that calls ``VerticalPath.run_trial``.
    """

    mode = "REPLAY"

    def execute(self, case: BatchCase, plan: BatchPlan) -> CaseExecution:
        record_id = f"trial-{case.order_index + 1:04d}"
        event_base = {"caseId": case.case_id, "trialId": record_id, "kernelPath": "assurance.kernel",
                      "mode": self.mode, "interface": "E2", "scope": {"kind": "UE", "id": case.profile.scope.get("ue", "ue-1")}}
        return CaseExecution(
            outcome="SUCCESS", validity="VALID",
            raw_events=(dict(event_base, eventKind="TrialOpened"),
                        dict(event_base, eventKind="EvidenceClosed", postClosureWitness=True)),
            measurements={"kpm": 10.0 + case.repeat_index, "o1": 9.0, "core": 8.0,
                          "ran": 7.0, "ueApplication": 6.0},
            wall_clock_s=1.0 + case.repeat_index / 10, closure_progress=1.0,
            closure_efficiency=1.0, harm_reserve=1.0, harm_charge=0.25,
            target_debt=0.0, target_satisfied=True, hold_satisfied=True,
            confirmations=1, post_closure_witnesses=1,
        )


@dataclass(frozen=True)
class BatchRunResult:
    run_dir: Path
    manifest: Mapping[str, Any]
    summary: Mapping[str, Any]


class BatchRunner:
    """Orchestrates confirmed BatchPlan cases without changing the Kernel route."""

    def __init__(self, *, executor: KernelCaseExecutor, runs_root: Path | str) -> None:
        self._executor = executor
        self._runs_root = Path(runs_root)

    def run(self, plan: BatchPlan, *, confirmation: ConfirmationRecord) -> BatchRunResult:
        plan.require_confirmation(confirmation)
        mode = getattr(self._executor, "mode", None)
        if mode not in {"REPLAY", "EMULATED"}:
            # Live calls are intentionally out of this lane.  A live adapter may
            # share the executor protocol only in the dedicated deployment lane.
            raise ValueError("BatchRunner is hardware-free and accepts REPLAY/EMULATED executors only")
        mode_evidence = {"basis": "REPLAY_OF_RECORDED_SOURCE"} if mode == "REPLAY" else {"basis": "OFFLINE_MODEL"}
        store = SessionStore.create(self._runs_root, mode=mode, mode_evidence=mode_evidence,
                                    profile_id="batch", config_snapshot={"batchPlan": plan.canonical_dict()})
        try:
            store.set_contract(authority_version="gate-6-batch/1", bundle_digest=plan.content_hash)
            store.set_llm(backend_name="strategy-injected", model_version=plan.model.get("version"))
            store.add_source(source_id="batch-plan", kind="EXPERIMENT_RUN", boundary="LOCAL_PROCESS",
                             digest=plan.content_hash, schema_version="batch-plan/1", notes="confirmed BatchPlan")
            rows, raw_events = self._execute_cases(plan)
            raw_digest = self._write_raw_bundle(store.run_dir, [
                *raw_events,
                *(self._canonical_observation(row) for row in rows),
            ])
            self._append_common_schema(store, rows)
            metrics = derive_batch_metrics(rows)
            summary = dict(metrics)
            summary.update({"planContentHash": plan.content_hash, "confirmation": confirmation.to_canonical_dict(),
                            "rawBundle": {"path": "raw/batch/events.jsonl", "sha256": raw_digest},
                            "mode": mode, "measurementKinds": ["MEASURED", "DERIVED"],
                            "axes": {"measurementKind": "DERIVED", "interface": "O1",
                                     "scope": {"kind": "RUN", "id": store.run_id}, "mode": mode}})
            self._write_normalized(store.run_dir, rows)
            parquet = self._write_parquet_if_available(store.run_dir, rows)
            provenance = self._provenance(plan, mode, raw_digest, parquet)
            (store.run_dir / "summary" / "provenance-manifest.json").write_text(
                json.dumps(provenance, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
            index = metric_index_entry(status="OK", source="normalized/trials.json", scope_level="RUN",
                                       unit="trial", sample_count=len(rows), derived=True)
            index["axes"] = ["measurementKind", "interface", "scope", "mode"]
            store.write_metric_index({"batchTrials": index})
            store.write_summary(summary, statistics=metrics)
            self._write_figures(store, [row for row in rows if row["included"]])
            manifest = store.finalize("COMPLETED")
            return BatchRunResult(run_dir=store.run_dir, manifest=manifest, summary=summary)
        except Exception:
            if store.writable:
                store.finalize("FAILED")
            raise

    def _execute_cases(self, plan: BatchPlan) -> tuple[list[Dict[str, Any]], list[Dict[str, Any]]]:
        rows: list[Dict[str, Any]] = []
        raw_events: list[Dict[str, Any]] = []
        for case in plan.cases():
            attempts = 0
            while True:
                execution = self._executor.execute(case, plan)
                attempts += 1
                if execution.validity != "ERROR" or attempts > plan.retry.retries or plan.retry.abort_on_error:
                    break
            trial_id = f"trial-{case.order_index + 1:04d}"
            scope_id = str(case.profile.scope.get("ue") or case.profile.scope.get("cell") or case.profile.profile_id)
            scope_kind = "UE" if "ue" in case.profile.scope else ("CELL" if "cell" in case.profile.scope else "SLICE")
            record = {
                "recordId": trial_id, "caseId": case.case_id, "objective": case.objective,
                "profileId": case.profile.profile_id, "strategy": plan.strategy, "repeat": case.repeat_index,
                "seed": case.seed, "mode": self._executor.mode, "measurementKind": "MEASURED",
                "interface": "E2", "scope": {"kind": scope_kind, "id": scope_id},
                "outcome": execution.outcome, "validity": execution.validity, "wallClockS": execution.wall_clock_s,
                "recoveryS": execution.recovery_s, "rolledBack": execution.rolled_back, "incident": execution.incident,
                "closureProgress": execution.closure_progress, "closureEfficiency": execution.closure_efficiency,
                "harmReserve": execution.harm_reserve, "harmCharge": execution.harm_charge,
                "targetDebt": execution.target_debt, "harmLimitRespected": execution.harm_limit_respected,
                "invariantViolations": execution.invariant_violations, "proposalRejects": execution.proposal_rejects,
                "proposalStale": execution.proposal_stale, "fallbacks": execution.fallbacks,
                "agentTokens": execution.agent_tokens, "agentToolCalls": execution.agent_tool_calls,
                "agentLatencyMs": execution.agent_latency_ms, "targetSatisfied": execution.target_satisfied,
                "holdSatisfied": execution.hold_satisfied, "evidenceReuse": execution.evidence_reuse,
                "confirmations": execution.confirmations, "postClosureWitnesses": execution.post_closure_witnesses,
                **dict(execution.measurements),
            }
            record["inclusionRule"] = plan.retry.inclusion
            record["included"] = (
                True if plan.retry.inclusion == "all_terminal" else
                execution.validity == "VALID" if plan.retry.inclusion == "valid_only" else
                execution.validity != "ERROR"
            )
            rows.append(record)
            raw_events.extend(dict(event, recordId=trial_id, eventIndex=index,
                                   measurementKind="MEASURED")
                              for index, event in enumerate(execution.raw_events))
        return rows, raw_events

    @staticmethod
    def _canonical_observation(row: Mapping[str, Any]) -> Dict[str, Any]:
        """Retain every normalized-trial source value in the immutable raw bundle."""
        observation = dict(row)
        observation.update({
            "eventKind": "CanonicalTrialObservation",
            "observationSchema": "batch-canonical-trial/1",
            "derivedAxes": {
                "measurementKind": "DERIVED", "interface": "O1",
                "scope": dict(row["scope"]), "mode": row["mode"],
            },
        })
        return observation

    @staticmethod
    def _write_raw_bundle(run_dir: Path, events: Sequence[Mapping[str, Any]]) -> str:
        path = run_dir / "raw" / "batch" / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(json.dumps(event, sort_keys=True, allow_nan=False, separators=(",", ":")) + "\n" for event in events)
        path.write_text(payload, encoding="utf-8")
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _append_common_schema(store: SessionStore, rows: Sequence[Mapping[str, Any]]) -> None:
        for seq, row in enumerate(rows):
            scope = row["scope"]
            telemetry = telemetry_sample(
                seq=seq, metric="batch.target_satisfaction", value=1.0 if row["targetSatisfied"] else 0.0,
                unit="ratio", scope_level=scope["kind"], scope_id=scope["id"],
                boundary="EXPERIMENT_RECORD", interface=row["interface"], t_rel_s=float(seq), quality="OK",
            )
            telemetry.update({"measurementKind": "MEASURED", "mode": row["mode"], "recordId": row["recordId"]})
            store.append_telemetry(telemetry)
            terminal = "commit_original" if row["outcome"] == "SUCCESS" else "technical_failsafe"
            episode = episode_trace(episode_id=row["recordId"], intent_text=f"Batch {row['objective']}",
                                    terminal_outcome=terminal)
            episode.update({"measurementKind": "MEASURED", "interface": row["interface"],
                            "scope": scope, "mode": row["mode"]})
            store.append_episode(episode)
            event = timeline_event(seq=seq, lane="DECISION", kind="DECISION_COMPLETE", origin="DERIVED",
                                   derivation="batch_trial_summary", t_rel_s=float(seq),
                                   ids={"episodeId": row["recordId"]})
            event.update({"measurementKind": "DERIVED", "interface": "O1", "scope": scope,
                          "mode": row["mode"], "recordId": row["recordId"]})
            store.append_event(event)

    @staticmethod
    def _write_normalized(run_dir: Path, rows: Sequence[Mapping[str, Any]]) -> None:
        normalized = run_dir / "normalized"
        normalized.mkdir(parents=True, exist_ok=True)
        (normalized / "trials.json").write_text(json.dumps(list(rows), indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        flat = []
        for row in rows:
            copied = dict(row)
            copied["scopeKind"] = copied.pop("scope")["kind"]
            copied["scopeId"] = row["scope"]["id"]
            flat.append(copied)
        fields = sorted({key for row in flat for key in row})
        with (normalized / "trials.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="raise", lineterminator="\n")
            writer.writeheader()
            writer.writerows(flat)

    @staticmethod
    def _write_parquet_if_available(run_dir: Path, rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        status_path = run_dir / "normalized" / "parquet-status.json"
        try:
            import pyarrow as pa  # type: ignore[import-not-found]
            import pyarrow.parquet as pq  # type: ignore[import-not-found]
        except ImportError as exc:
            status = {"status": "UNAVAILABLE", "reason": "pyarrow is not installed", "install": "pip install pyarrow",
                      "fallback": ["normalized/trials.csv", "normalized/trials.json"]}
        else:
            pq.write_table(pa.Table.from_pylist(list(rows)), run_dir / "normalized" / "trials.parquet")
            status = {"status": "OK", "path": "normalized/trials.parquet"}
        status_path.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return status

    @staticmethod
    def _provenance(plan: BatchPlan, mode: str, raw_digest: str, parquet: Mapping[str, Any]) -> Dict[str, Any]:
        return {"schemaVersion": "batch-provenance/1", "mode": mode, "rawBundleSha256": raw_digest,
                "environment": {"python": platform.python_version(), "platform": platform.platform()},
                "software": {"batch": "assurance.batch/1", "sessionStore": "oran-aic-phase-b-gui-session/1.0.0"},
                "contracts": {"batchPlanContentHash": plan.content_hash}, "model": dict(plan.model),
                "topology": dict(plan.topology), "hardware": {"liveCalls": 0, "adapter": "mock"},
                "parquet": dict(parquet)}

    def _write_figures(self, store: SessionStore, rows: Sequence[Mapping[str, Any]]) -> None:
        import matplotlib
        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot as plt

        figures = store.run_dir / "figures"
        values = [float(row["wallClockS"]) for row in rows]
        outcomes = [str(row["outcome"]) for row in rows]
        data = {
            "cdf": (_FIGURE_DERIVATIONS["cdf"], lambda ax: self._cdf(ax, values)),
            "box": (_FIGURE_DERIVATIONS["box"], lambda ax: ax.boxplot(values)),
            "violin": (_FIGURE_DERIVATIONS["violin"], lambda ax: ax.violinplot(values, showmeans=True)),
            "convergence": (_FIGURE_DERIVATIONS["convergence"], lambda ax: ax.plot(range(1, len(rows) + 1), [row["closureProgress"] for row in rows], color=_PALETTE[0])),
            "harm_efficiency": (_FIGURE_DERIVATIONS["harm_efficiency"], lambda ax: ax.scatter([row["closureEfficiency"] for row in rows], [row["harmCharge"] for row in rows], color=_PALETTE[1])),
            "stacked_outcome": (_FIGURE_DERIVATIONS["stacked_outcome"], lambda ax: self._outcome_bar(ax, outcomes)),
            "timeline": (_FIGURE_DERIVATIONS["timeline"], lambda ax: ax.plot(range(1, len(rows) + 1), values, marker="o", color=_PALETTE[2])),
        }
        for figure_id, (derivation, draw) in data.items():
            fig, axis = plt.subplots(figsize=(5, 3))
            if rows:
                draw(axis)
            else:
                axis.text(0.5, 0.5, "No included trials", transform=axis.transAxes,
                          ha="center", va="center")
            axis.set_title(figure_id.replace("_", " "))
            axis.set_xlabel("trial")
            axis.set_ylabel("value")
            if store.mode != "LIVE":
                axis.text(0.5, 0.5, "NOT LIVE", transform=axis.transAxes, ha="center", va="center",
                          fontsize=22, color="#777777", alpha=0.25, rotation=25)
            paths = []
            for extension in ("svg", "pdf", "png"):
                path = figures / f"{figure_id}.{extension}"
                fig.savefig(path, dpi=300, bbox_inches="tight")
                if extension == "svg":
                    # Matplotlib's pretty-printed SVG paths intentionally retain
                    # visual-space suffixes.  Strip them so the paper artifact is
                    # clean under the repository's whitespace gate.
                    path.write_text("\n".join(line.rstrip() for line in path.read_text(encoding="utf-8").splitlines()) + "\n",
                                    encoding="utf-8")
                paths.append(str(path.relative_to(store.run_dir)))
            plt.close(fig)
            csv_path, trace = self._write_figure_source_and_traceability(
                store.run_dir, figure_id, rows, derivation, store.mode,
            )
            sidecar = figures / f"{figure_id}.traceability.json"
            store.write_figure(figure_id, paths=paths, source_csv=str(csv_path.relative_to(store.run_dir)),
                               metadata={"processing": {"smoothing": "none", "exclusions": "BatchPlan inclusion rule", "aggregation": derivation, "missingValueHandling": "excluded"},
                                         "series": [{"name": figure_id, "n": len(rows)}], "traceability": str(sidecar.relative_to(store.run_dir)), "axes": trace["axes"]})
        self._write_latex_table(store.run_dir, rows)

    @staticmethod
    def _cdf(axis: Any, values: Sequence[float]) -> None:
        ordered = sorted(values)
        axis.step(ordered, [(index + 1) / len(ordered) for index in range(len(ordered))], where="post", color=_PALETTE[0])

    @staticmethod
    def _outcome_bar(axis: Any, outcomes: Sequence[str]) -> None:
        labels = sorted(set(outcomes))
        axis.bar(labels, [outcomes.count(label) for label in labels], color=_PALETTE[:len(labels)])

    @staticmethod
    def _write_latex_table(run_dir: Path, rows: Sequence[Mapping[str, Any]]) -> None:
        path = run_dir / "summary" / "batch-summary.tex"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["\\begin{tabular}{lrr}", "Outcome & Trials & Mean wall-clock (s) \\\\", "\\hline"]
        for outcome in sorted({str(row["outcome"]) for row in rows}):
            selected = [float(row["wallClockS"]) for row in rows if row["outcome"] == outcome]
            lines.append(f"{outcome} & {len(selected)} & {sum(selected) / len(selected):.3f} \\\\")
        lines.append("\\end{tabular}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        trace = {"sourceRecordIds": [str(row["recordId"]) for row in rows],
                 "derivation": "group_by(outcome);mean(wallClockS)",
                 "axes": {"measurementKind": "DERIVED", "interface": "O1",
                          "scopeKinds": sorted({row["scope"]["kind"] for row in rows}),
                          "mode": rows[0]["mode"] if rows else None}}
        (run_dir / "summary" / "batch-summary.traceability.json").write_text(
            json.dumps(trace, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @staticmethod
    def _write_figure_source_and_traceability(
        run_dir: Path, figure_id: str, rows: Sequence[Mapping[str, Any]], derivation: str, mode: str,
    ) -> tuple[Path, Mapping[str, Any]]:
        figures = run_dir / "figures"
        figures.mkdir(parents=True, exist_ok=True)
        csv_path = figures / f"{figure_id}.source.csv"
        csv_path.write_text(
            "recordId,wallClockS\n" + "".join(f"{row['recordId']},{row['wallClockS']}\n" for row in rows),
            encoding="utf-8",
        )
        trace = {
            "sourceRecordIds": [str(row["recordId"]) for row in rows],
            "derivation": derivation,
            "axes": {
                "measurementKind": "DERIVED", "interface": "O1",
                "scopeKinds": sorted({row["scope"]["kind"] for row in rows}), "mode": mode,
            },
        }
        (figures / f"{figure_id}.traceability.json").write_text(
            json.dumps(trace, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
        return csv_path, trace
