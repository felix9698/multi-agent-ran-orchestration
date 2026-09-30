"""SessionStore - the one run directory format.

Body owner: track **T3** (see ``docs/phase-b-gui/file-ownership.1.0.0.json``,
which still names the re-export path ``gui/operator/store/session_store.py``;
the frozen surface below is the one it describes).

Neutral by location as well as by dependency: it lives at the top level so
``assurance/batch/`` can share the one run-directory schema without importing a
console (Gate 2).  ``gui/operator/store/session_store.py`` re-exports it unchanged.

Schema authority: ``docs/phase-b-gui/session-store.1.0.0.schema.json``.

One run directory format for every mode.  Live, Replay, Synthetic and Emulated
write the *same* structure and differ only in ``manifest.mode`` and in the
provenance recorded per record.  That is what makes a workspace able to render a
replayed run and a live one with no code path of its own, and it is what
phaseB_task.md section 9 requires.

Layout::

    <runs_root>/<runId>/
      manifest.json          identity, mode, disposition, sources, artifacts, issues
      config-snapshot.json   effective config at start; credential REFERENCES only
      raw/<sourceId>/**      verbatim upstream artifacts, byte-preserved
      normalized/telemetry.jsonl
      normalized/metric-index.json
      decision/episodes.jsonl
      decision/cycles.jsonl
      decision/llm-calls.jsonl
      events/timeline.jsonl
      summary/summary.json
      summary/statistics.json
      figures/<id>.{pdf,svg,png}, <id>.source.csv, <id>.meta.json
      gui-state.json

Four implementation rules carry the design's honesty guarantees:

* ``.jsonl`` files are append-only and flushed per record, so a crash truncates
  at most the final line.
* ``manifest.json`` is written **last** and **atomically** (temp file plus
  ``os.replace``).  A directory without a finalized manifest re-opens as
  ``INTERRUPTED`` with every artifact preserved - an errored or aborted run is
  never reported as success.
* ``None`` means honest-unknown.  It is written as JSON ``null``, JSON is emitted
  with ``allow_nan=False`` matching the experiments runner, and nothing is
  coerced to ``0``.
* No credential value is ever written.  A config snapshot is scrubbed on the way
  in: a credential-shaped key whose value is not already a reference is replaced
  before it reaches disk, so the secret-scan gate has nothing to find and a
  mistake upstream cannot leak through the store.

This module must never import ``tkinter``, ``subprocess`` or ``socket``.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Final, Iterator, List, Mapping, Optional, Sequence

SCHEMA_VERSION: Final[str] = "oran-aic-phase-b-gui-session/1.0.0"

#: Written into every manifest so a stored run records the console that made it.
GUI_VERSION: Final[str] = "phase-b-operator-console/1.0.0"

MODES: Final[tuple] = ("LIVE", "REPLAY", "SYNTHETIC", "EMULATED")
DISPOSITIONS: Final[tuple] = ("RUNNING", "COMPLETED", "ABORTED", "FAILED",
                              "INTERRUPTED")

#: Only COMPLETED is a success.  Every other disposition keeps its data and
#: renders as non-success.
SUCCESS_DISPOSITIONS: Final[tuple] = ("COMPLETED",)

ISSUE_KINDS: Final[tuple] = (
    "MISSING_METRIC", "STALE_DATA", "PARTIAL_DATA", "SCHEMA_MISMATCH",
    "CALLBACK_FAILURE", "CONNECTION_LOST", "DATA_TRUNCATED",
    "UNSUPPORTED_FIELD", "DANGLING_REFERENCE",
)

MODE_EVIDENCE_BASES: Final[tuple] = (
    "LIVE_R1_TRANSPORT", "REPLAY_OF_RECORDED_SOURCE", "OFFLINE_MODEL",
)

SOURCE_KINDS: Final[tuple] = (
    "R1_LIVE", "LO1_CAPTURE", "LO1_EVIDENCE_BUNDLE", "EXPERIMENT_RUN",
    "CAPABILITY_MANIFEST", "GUI_LOCAL",
)

SOURCE_BOUNDARIES: Final[tuple] = (
    "RAPP_INTERNAL", "R1_POLICY", "R1_DME", "O1_ASSURANCE",
    "CAPABILITY_MANIFEST", "LOCAL_PROCESS", "SESSION_STORE",
)

#: Relative paths the schema declares as required entries of a run directory.
REQUIRED_ENTRIES: Final[tuple] = (
    "config-snapshot.json", "normalized/telemetry.jsonl",
    "normalized/metric-index.json", "decision/episodes.jsonl",
    "decision/cycles.jsonl", "decision/llm-calls.jsonl",
    "events/timeline.jsonl", "summary/summary.json",
)

_JSONL: Final[Dict[str, str]] = {
    "telemetry": "normalized/telemetry.jsonl",
    "events": "events/timeline.jsonl",
    "episodes": "decision/episodes.jsonl",
    "cycles": "decision/cycles.jsonl",
    "llm": "decision/llm-calls.jsonl",
}

#: A key whose name matches is credential-shaped.  A value under one of these is
#: written only when it is already a *reference*; otherwise it never reaches disk.
_CREDENTIAL_KEY_MARKERS: Final[tuple] = (
    "key", "token", "secret", "password", "passwd", "credential", "apikey",
    "auth", "bearer", "private",
)

#: Value prefixes that are references rather than material.
_REFERENCE_PREFIXES: Final[tuple] = ("env:", "secret://", "ref:", "file:", "keyring:")

REDACTED: Final[str] = "REDACTED_NEVER_WRITTEN"


class SessionStoreError(RuntimeError):
    """A store operation that would violate the run-directory contract."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _compact_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _dumps(payload: Any) -> str:
    """Strict JSON.  ``allow_nan=False`` matches the experiments runner, so a
    reader may use the standard-library parser without a NaN special case."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, separators=(",", ":"))


def _is_reference(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_REFERENCE_PREFIXES)


def scrub_credentials(node: Any) -> Any:
    """Replace credential-shaped values with a marker, recursively.

    Deliberately not an exception: a config snapshot arrives while a run is
    starting, and refusing it would abort the experiment over a naming mistake
    upstream.  Replacing the value keeps the run alive, keeps the *shape* of the
    configuration visible, and leaves the secret scan nothing to find.  A value
    that is already a reference (``env:ANTHROPIC_API_KEY``, ``secret://sftp``) is
    kept, because a reference name is exactly what the Settings view shows.
    """
    if isinstance(node, Mapping):
        scrubbed: Dict[str, Any] = {}
        for key, value in node.items():
            lowered = str(key).lower()
            looks_credential = any(m in lowered for m in _CREDENTIAL_KEY_MARKERS)
            if looks_credential and isinstance(value, str) and not _is_reference(value):
                scrubbed[key] = REDACTED
            elif looks_credential and isinstance(value, (int, float)) \
                    and not isinstance(value, bool):
                scrubbed[key] = REDACTED
            else:
                scrubbed[key] = scrub_credentials(value)
        return scrubbed
    if isinstance(node, list):
        return [scrub_credentials(v) for v in node]
    if isinstance(node, tuple):
        return [scrub_credentials(v) for v in node]
    return node


def _repo_commit() -> Optional[str]:
    """Best-effort repository commit, read from ``.git`` without a subprocess.

    ``subprocess`` is forbidden anywhere in the console subtree, and shelling out
    to ``git`` for a provenance field would be a boundary violation for a nicety.
    Any problem yields ``None``, which the manifest carries honestly.
    """
    try:
        root = Path(__file__).resolve().parents[1]
        git = root / ".git"
        if git.is_file():                       # worktree: .git is a pointer file
            text = git.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir:"):
                return None
            git = Path(text.split(":", 1)[1].strip())
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head or None
        ref = head.split(":", 1)[1].strip()
        ref_path = git / ref
        if ref_path.is_file():
            return ref_path.read_text(encoding="utf-8").strip() or None
        packed = git / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + ref):
                    return line.split(" ", 1)[0]
        # a worktree keeps its own HEAD but shares the common object refs
        common = git / "commondir"
        if common.is_file():
            shared = (git / common.read_text(encoding="utf-8").strip()).resolve()
            ref_path = shared / ref
            if ref_path.is_file():
                return ref_path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None
    return None


class SessionStore:
    """Writer and reader for one run directory."""

    SCHEMA_VERSION: Final[str] = SCHEMA_VERSION

    def __init__(self, run_dir: Path, manifest: Dict[str, Any], *,
                 writable: bool, analysis: bool = False) -> None:
        self._run_dir = Path(run_dir)
        self._manifest = manifest
        self._writable = writable
        #: Analysis mode: a finalized run may gain FIGURES and GUI state and
        #: nothing else.  Evidence - raw/, normalized/, decision/, events/ - is
        #: never rewritten, so a stored run stays the thing it recorded.
        self._analysis = analysis
        self._handles: Dict[str, Any] = {}
        self._counts: Dict[str, int] = dict(manifest.get("counts") or {})
        self._issues: List[Dict[str, Any]] = list(manifest.get("dataIssues") or [])
        self._figures: Dict[str, Dict[str, Any]] = {}
        self._started = time.monotonic()

    # -- lifecycle ---------------------------------------------------------- #

    @classmethod
    def create(cls, runs_root, *, mode: str, mode_evidence: Mapping[str, Any],
               profile_id=None, config_snapshot=None) -> "SessionStore":
        """Open a new run directory.

        ``mode`` comes from the adapter or the live source, never from the UI -
        there is deliberately no control that changes it, because a Replay run
        presented as LIVE is the worst failure this design guards against.
        ``mode_evidence`` records why the mode is claimed, and a LIVE run whose
        basis is not a live transport is refused outright.
        """
        if mode not in MODES:
            raise SessionStoreError(f"unknown mode {mode!r}")
        basis = (mode_evidence or {}).get("basis")
        if basis not in MODE_EVIDENCE_BASES:
            raise SessionStoreError(
                f"mode evidence basis {basis!r} is not one of {MODE_EVIDENCE_BASES}")
        # ``mode`` is the mode of THIS session, not of whatever was recorded.
        # Reading a recording back is a Replay session even when the recording
        # was made against a live radio - otherwise the export, the watermark
        # and the badge all say LIVE for data nobody is observing.  The
        # recording's own mode travels separately, as ``sourceMode``.
        #
        # So the two rules are symmetric and admit no exception:
        #   LIVE  <=> the data arrived over a live transport, now.
        #   REPLAY <=> the data came out of a recording.
        if basis == "LIVE_R1_TRANSPORT" and mode != "LIVE":
            raise SessionStoreError(
                f"LIVE_R1_TRANSPORT evidence cannot back a {mode} run")
        if mode == "LIVE" and basis != "LIVE_R1_TRANSPORT":
            raise SessionStoreError(
                "a LIVE run must be evidenced by LIVE_R1_TRANSPORT; a run read "
                f"back from a recording is a Replay session, not LIVE ({basis})")
        if mode == "REPLAY" and basis != "REPLAY_OF_RECORDED_SOURCE":
            raise SessionStoreError(
                f"a REPLAY run must be evidenced by REPLAY_OF_RECORDED_SOURCE, "
                f"not {basis}")

        run_id = f"{mode.lower()}-{_compact_utc()}-{secrets.token_hex(3)}"
        run_dir = Path(runs_root) / run_id
        if run_dir.exists():
            raise SessionStoreError(f"run directory already exists: {run_dir}")
        for sub in ("raw", "normalized", "decision", "events", "summary", "figures"):
            (run_dir / sub).mkdir(parents=True, exist_ok=True)

        now = _utc_now()
        manifest: Dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION,
            "runId": run_id,
            "mode": mode,
            "modeEvidence": dict(mode_evidence),
            "disposition": "RUNNING",
            "createdAt": now,
            "startedAt": now,
            "endedAt": None,
            "elapsedS": None,
            "profileId": profile_id,
            "profileDigest": None,
            "guiVersion": GUI_VERSION,
            "repoCommit": _repo_commit(),
            "contractAuthorityVersion": None,
            "contractBundleDigest": None,
            "operatorNote": None,
            "sources": [],
            "llm": {"backendName": None, "modelVersion": None,
                    "switchAudit": [], "credentialRefs": []},
            "artifacts": {},
            "counts": {},
            "dataIssues": [],
        }

        snapshot = scrub_credentials(dict(config_snapshot or {}))
        (run_dir / "config-snapshot.json").write_text(
            json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False,
                       allow_nan=False) + "\n", encoding="utf-8")
        if profile_id is not None:
            manifest["profileDigest"] = hashlib.sha256(
                _dumps(snapshot).encode("utf-8")).hexdigest()

        store = cls(run_dir, manifest, writable=True)
        for rel in _JSONL.values():             # exist even when empty
            (run_dir / rel).touch()
        # The schema declares these two as required entries of a run directory.
        # They are created empty so a crashed run still has the layout a reader
        # expects; an empty index means "nothing was indexed", which is a fact,
        # not a substitute for one.
        for rel in ("normalized/metric-index.json", "summary/summary.json"):
            (run_dir / rel).write_text("{}\n", encoding="utf-8")
        return store

    @classmethod
    def open(cls, run_dir) -> "SessionStore":
        """Re-open a finalized or interrupted run, read-only.

        This is what makes phaseB_task.md section 9's "re-open a completed run
        after a GUI restart" work: every workspace repopulates from the directory
        alone, with no live source involved.

        A directory with no finalized manifest is not an error.  It re-opens as
        ``INTERRUPTED`` with a synthesized header and every artifact preserved -
        and nothing is written, so re-opening an interrupted run never changes
        it.
        """
        path = Path(run_dir)
        if not path.is_dir():
            raise SessionStoreError(f"run directory not found: {path}")
        manifest_path = path / "manifest.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                manifest = cls._interrupted_header(path)
                manifest["dataIssues"].append({
                    "kind": "DATA_TRUNCATED",
                    "detail": "manifest.json is not valid JSON; the run is "
                              "re-opened as INTERRUPTED with its artifacts intact",
                    "at": None, "gapId": None})
        else:
            manifest = cls._interrupted_header(path)
        return cls(path, manifest, writable=False)

    @classmethod
    def reopen_for_analysis(cls, run_dir) -> "SessionStore":
        """Re-open a finalized run so figures and GUI state can be added.

        A finalized run is evidence and its records are never rewritten.  But a
        figure is *derived from* that evidence, not part of it, and the schema
        puts ``figures/`` inside the run directory precisely so a figure travels
        with the data it was drawn from.  This mode therefore permits
        ``write_figure`` and ``save_gui_state`` and refuses every append.

        ``finalize_analysis`` then rewrites ``manifest.json`` atomically with a
        refreshed artifact index, so the index never claims a set of files that
        is no longer what is on disk.  The disposition is preserved: adding a
        figure to an aborted run does not make it a successful one.
        """
        store = cls.open(run_dir)
        if store._manifest.get("disposition") == "RUNNING":
            raise SessionStoreError(
                "a still-running run is written through its own store, not "
                "re-opened for analysis")
        store._writable = True
        store._analysis = True
        return store

    def finalize_analysis(self) -> Mapping[str, Any]:
        """Rewrite the manifest after an analysis session, preserving disposition."""
        if not self._analysis:
            raise SessionStoreError("this store is not in analysis mode")
        self.close()
        self._manifest["artifacts"] = self._index_artifacts()
        self._manifest["dataIssues"] = list(self._issues)
        payload = json.dumps(self._manifest, indent=2, sort_keys=True,
                             ensure_ascii=False, allow_nan=False) + "\n"
        target = self._run_dir / "manifest.json"
        tmp = target.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        self._writable = False
        self._analysis = False
        return dict(self._manifest)

    @staticmethod
    def _interrupted_header(path: Path) -> Dict[str, Any]:
        """Synthesize a header for a run whose finalize was never observed."""
        return {
            "schemaVersion": SCHEMA_VERSION,
            "runId": path.name,
            "mode": "REPLAY" if path.name.startswith("replay-") else
                    ("SYNTHETIC" if path.name.startswith("synthetic-") else
                     ("EMULATED" if path.name.startswith("emulated-") else
                      ("LIVE" if path.name.startswith("live-") else "REPLAY"))),
            "disposition": "INTERRUPTED",
            "createdAt": _utc_now(),
            "guiVersion": GUI_VERSION,
            "sources": [], "artifacts": {}, "counts": {},
            "dataIssues": [{
                "kind": "PARTIAL_DATA",
                "detail": "no finalized manifest was found; the run is presented "
                          "as INTERRUPTED and is never summarized as success",
                "at": None, "gapId": None}],
        }

    @staticmethod
    def list_runs(runs_root) -> list:
        """Manifest headers under ``runs_root``, newest first."""
        root = Path(runs_root)
        if not root.is_dir():
            return []
        headers = []
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            try:
                store = SessionStore.open(child)
            except SessionStoreError:
                continue
            manifest = store._manifest
            headers.append({
                "runId": manifest.get("runId", child.name),
                "mode": manifest.get("mode"),
                "disposition": manifest.get("disposition"),
                "createdAt": manifest.get("createdAt"),
                "startedAt": manifest.get("startedAt"),
                "endedAt": manifest.get("endedAt"),
                "profileId": manifest.get("profileId"),
                "counts": manifest.get("counts", {}),
                "issueCount": len(manifest.get("dataIssues") or []),
                "path": str(child),
            })
        headers.sort(key=lambda h: (h.get("createdAt") or "", h["runId"]),
                     reverse=True)
        return headers

    def finalize(self, disposition: str) -> Mapping[str, Any]:
        """Write ``manifest.json`` atomically and return it.

        The manifest is written *last* so its presence is the signal that the run
        completed its write path.  A partially written run has no manifest and
        therefore cannot be read as COMPLETED by anything.
        """
        self._require_writable()
        if disposition not in DISPOSITIONS or disposition == "RUNNING":
            raise SessionStoreError(f"cannot finalize as {disposition!r}")
        if self._manifest["disposition"] != "RUNNING":
            return dict(self._manifest)

        for handle in self._handles.values():
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
        self._handles.clear()

        self._manifest["disposition"] = disposition
        self._manifest["endedAt"] = _utc_now()
        self._manifest["elapsedS"] = round(time.monotonic() - self._started, 3)
        self._manifest["counts"] = dict(self._counts)
        self._manifest["dataIssues"] = list(self._issues)
        self._manifest["artifacts"] = self._index_artifacts()

        payload = json.dumps(self._manifest, indent=2, sort_keys=True,
                             ensure_ascii=False, allow_nan=False) + "\n"
        target = self._run_dir / "manifest.json"
        tmp = target.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        self._writable = False
        return dict(self._manifest)

    def close(self) -> None:
        """Flush and close the append handles without finalizing.

        A run that is closed but not finalized stays INTERRUPTED, which is the
        correct reading: closing a file is not the same as declaring an outcome.
        """
        for handle in self._handles.values():
            try:
                handle.flush()
                handle.close()
            except (OSError, ValueError):        # already closed / gone
                pass
        self._handles.clear()

    def __del__(self) -> None:                   # pragma: no cover - GC timing
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self) -> "SessionStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        """Finalize as ``INTERRUPTED`` if the run is still ``RUNNING``.

        An exception on the way out does not become a successful run: the
        disposition records that finalize was never reached deliberately.
        """
        if self._writable and self._manifest["disposition"] == "RUNNING":
            self.finalize("INTERRUPTED")

    # -- identity ----------------------------------------------------------- #

    @property
    def run_id(self) -> str:
        return str(self._manifest["runId"])

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    @property
    def mode(self) -> str:
        return str(self._manifest["mode"])

    @property
    def disposition(self) -> str:
        return str(self._manifest["disposition"])

    @property
    def is_success(self) -> bool:
        return self.disposition in SUCCESS_DISPOSITIONS

    @property
    def writable(self) -> bool:
        return self._writable

    # -- provenance --------------------------------------------------------- #

    def add_source(self, *, source_id: str, kind: str, boundary: str,
                   path: Optional[str] = None, digest: Optional[str] = None,
                   schema_version: Optional[str] = None,
                   adapter: Optional[str] = None,
                   notes: Optional[str] = None) -> None:
        """Record one upstream source and the boundary it came through.

        Additive to the frozen surface: the manifest carries ``sources`` and
        ``create`` takes none, so an adapter needs a way to declare what it read.
        """
        self._require_writable()
        if kind not in SOURCE_KINDS:
            raise SessionStoreError(f"unknown source kind {kind!r}")
        if boundary not in SOURCE_BOUNDARIES:
            raise SessionStoreError(f"unknown source boundary {boundary!r}")
        self._manifest["sources"].append({
            "sourceId": source_id, "kind": kind, "boundary": boundary,
            "path": path, "digest": digest, "schemaVersion": schema_version,
            "adapter": adapter, "notes": notes,
        })

    def set_llm(self, *, backend_name: Optional[str] = None,
                model_version: Optional[str] = None,
                credential_refs: Sequence[str] = (),
                switch_audit: Sequence[Mapping[str, Any]] = ()) -> None:
        """Record model identity.  ``credential_refs`` holds reference NAMES only.

        A value that is not reference-shaped is refused rather than scrubbed: a
        caller reaching this method is explicitly naming credentials, so a
        mistake here should be loud.
        """
        self._require_writable()
        for ref in credential_refs:
            if not _is_reference(ref):
                raise SessionStoreError(
                    f"credentialRefs holds reference names only, not {ref!r}")
        self._manifest["llm"] = {
            "backendName": backend_name,
            "modelVersion": model_version,
            "switchAudit": [dict(entry) for entry in switch_audit],
            "credentialRefs": list(credential_refs),
        }

    def set_contract(self, *, authority_version: Optional[str] = None,
                     bundle_digest: Optional[str] = None) -> None:
        self._require_writable()
        self._manifest["contractAuthorityVersion"] = authority_version
        self._manifest["contractBundleDigest"] = bundle_digest

    # -- append ------------------------------------------------------------- #

    def append_telemetry(self, sample: Mapping[str, Any]) -> None:
        """Append one TelemetrySample to ``normalized/telemetry.jsonl``."""
        self._append("telemetry", sample, "telemetrySamples")

    def append_event(self, event: Mapping[str, Any]) -> None:
        """Append one TimelineEvent to ``events/timeline.jsonl``."""
        self._append("events", event, "timelineEvents")
        severity = event.get("severity")
        if severity == "WARNING":
            self._counts["warnings"] = self._counts.get("warnings", 0) + 1
        elif severity == "ERROR":
            self._counts["errors"] = self._counts.get("errors", 0) + 1

    def append_episode(self, episode: Mapping[str, Any]) -> None:
        self._append("episodes", episode, "episodes")

    def append_cycle(self, cycle: Mapping[str, Any]) -> None:
        self._append("cycles", cycle, "cycles")

    def append_llm_call(self, call: Mapping[str, Any]) -> None:
        """Append one LlmCallTrace.

        Deliberately content-free: model identity, latency, prompt hash and
        schema validity only.  ``runstore.records.llm_call_trace``
        refuses model text on the way in; this is the second gate, because the
        store is what actually writes to disk.
        """
        from runstore.records import FORBIDDEN_LLM_FIELDS

        for name in call:
            if name in FORBIDDEN_LLM_FIELDS:
                raise SessionStoreError(
                    f"model text is forbidden in an LLM call record: {name!r}")
        self._append("llm", call, "llmCalls")

    def record_issue(self, kind: str, detail: str, *, at=None,
                     gap_id=None) -> None:
        """Record a data-quality issue on the manifest.

        Issues surface in the results view.  A run with missing, stale or partial
        data must show it rather than present a clean summary, which is why the
        issue list is part of the manifest and not a log line.
        """
        self._require_writable()
        if kind not in ISSUE_KINDS:
            raise SessionStoreError(f"unknown data-issue kind {kind!r}")
        self._issues.append({"kind": kind, "detail": detail, "at": at,
                             "gapId": gap_id})

    def copy_raw(self, source_id: str, src, rel: str) -> str:
        """Copy an upstream artifact byte-for-byte into ``raw/<source_id>/<rel>``.

        Returns the path relative to the run directory.  The copy is never edited
        afterwards - it is the evidence the normalized records point back to.
        """
        self._require_writable()
        target = self._run_dir / "raw" / source_id / rel
        resolved = target.resolve()
        base = (self._run_dir / "raw").resolve()
        if base not in resolved.parents:
            raise SessionStoreError(f"raw path escapes the run directory: {rel!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(Path(src).read_bytes())
        return str(target.relative_to(self._run_dir))

    # -- write whole documents ---------------------------------------------- #

    def write_metric_index(self, index: Mapping[str, Any]) -> None:
        self._write_json("normalized/metric-index.json", index)

    def write_summary(self, summary: Mapping[str, Any],
                      statistics=None) -> None:
        """Write the run summary and, when there is one, its statistics.

        ``summary/statistics.json`` is optional by schema and its absence is
        shown rather than substituted: a run with too few samples has no
        statistics, and inventing them would be the worst kind of quiet failure.
        """
        self._write_json("summary/summary.json", summary)
        if statistics is not None:
            self._write_json("summary/statistics.json", statistics)

    def write_figure(self, figure_id: str, *, paths: Sequence[str],
                     source_csv: str, metadata: Mapping[str, Any]) -> None:
        """Register an exported figure with its source data and metadata.

        The metadata ``processing`` block is mandatory and must state smoothing,
        exclusions, aggregation and missing-value handling - ``"none"`` is a
        valid, explicit value.  phaseB_task.md section 4 forbids hiding these,
        and refusing the write is how that is enforced rather than hoped for.
        """
        self._require_derivable()
        processing = (metadata or {}).get("processing")
        required = ("smoothing", "exclusions", "aggregation", "missingValueHandling")
        if not isinstance(processing, Mapping) or any(
                not processing.get(field) for field in required):
            raise SessionStoreError(
                f"figure {figure_id}: the processing block must state "
                + ", ".join(required) + " ('none' is a valid explicit value)")
        for series in metadata.get("series") or ():
            if "n" not in series:
                raise SessionStoreError(
                    f"figure {figure_id}: every series must carry its sample count n")
        self._write_json(f"figures/{figure_id}.meta.json", metadata,
                         derived=True)
        self._figures[figure_id] = {"paths": list(paths), "sourceCsv": source_csv}

    def save_gui_state(self, state: Mapping[str, Any]) -> None:
        """Persist workspace/selection state.  Cosmetic; never a statistics input."""
        self._write_json("gui-state.json", state, derived=True)

    # -- read --------------------------------------------------------------- #

    def read_telemetry(self, *, metric=None,
                       scope_id=None) -> Iterator[Mapping[str, Any]]:
        for record in self._read("telemetry"):
            if metric is not None and record.get("metric") != metric:
                continue
            if scope_id is not None and \
                    (record.get("scope") or {}).get("id") != scope_id:
                continue
            yield record

    def read_events(self, *, lane=None) -> Iterator[Mapping[str, Any]]:
        for record in self._read("events"):
            if lane is None or record.get("lane") == lane:
                yield record

    def read_episodes(self) -> Iterator[Mapping[str, Any]]:
        return self._read("episodes")

    def read_cycles(self) -> Iterator[Mapping[str, Any]]:
        return self._read("cycles")

    def read_llm_calls(self) -> Iterator[Mapping[str, Any]]:
        return self._read("llm")

    def read_manifest(self) -> Mapping[str, Any]:
        manifest = dict(self._manifest)
        manifest["counts"] = dict(self._counts)
        manifest["dataIssues"] = list(self._issues)
        return manifest

    def read_config_snapshot(self) -> Mapping[str, Any]:
        path = self._run_dir / "config-snapshot.json"
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def read_metric_index(self) -> Mapping[str, Any]:
        path = self._run_dir / "normalized" / "metric-index.json"
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def read_summary(self) -> Mapping[str, Any]:
        path = self._run_dir / "summary" / "summary.json"
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def read_statistics(self) -> Optional[Mapping[str, Any]]:
        path = self._run_dir / "summary" / "statistics.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def data_issues(self) -> List[Mapping[str, Any]]:
        """Issues from the manifest plus any observed while reading."""
        return list(self._issues)

    # -- internals ---------------------------------------------------------- #

    def _require_writable(self) -> None:
        if not self._writable:
            raise SessionStoreError(
                "this run is open read-only; a stored run is evidence and is "
                "never rewritten")
        if self._analysis:
            raise SessionStoreError(
                "this run is open for analysis; only figures and GUI state may "
                "be added to a finalized run, never a record")

    def _require_derivable(self) -> None:
        """Writable in either mode: figures and GUI state are derived, not evidence."""
        if not self._writable:
            raise SessionStoreError(
                "this run is open read-only; re-open it for analysis to add a "
                "figure")

    def _handle(self, name: str):
        handle = self._handles.get(name)
        if handle is None:
            path = self._run_dir / _JSONL[name]
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a", encoding="utf-8")
            self._handles[name] = handle
        return handle

    def _append(self, name: str, record: Mapping[str, Any],
                count_key: str) -> None:
        self._require_writable()
        handle = self._handle(name)
        handle.write(_dumps(record) + "\n")
        handle.flush()                       # per-record: a crash costs one line
        self._counts[count_key] = self._counts.get(count_key, 0) + 1

    def _read(self, name: str) -> Iterator[Mapping[str, Any]]:
        """Stream one jsonl file, tolerating a truncated final line.

        A crash truncates at most the last line.  Skipping it and recording a
        ``DATA_TRUNCATED`` issue keeps every complete record readable, which is
        the whole point of the append-only format; failing the load would throw
        away the evidence the format exists to preserve.
        """
        path = self._run_dir / _JSONL[name]
        if not path.is_file():
            return
        handle = self._handles.get(name)
        if handle is not None:
            handle.flush()
        with path.open("r", encoding="utf-8") as source:
            for lineno, line in enumerate(source, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    self._note_truncation(path.name, lineno)

    def _note_truncation(self, filename: str, lineno: int) -> None:
        detail = f"{filename}: line {lineno} is truncated and was skipped"
        if any(i["detail"] == detail for i in self._issues):
            return
        self._issues.append({"kind": "DATA_TRUNCATED", "detail": detail,
                             "at": None, "gapId": None})

    def _write_json(self, rel: str, payload: Mapping[str, Any], *,
                    derived: bool = False) -> None:
        if derived:
            self._require_derivable()
        else:
            self._require_writable()
        target = self._run_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False,
                       allow_nan=False) + "\n", encoding="utf-8")

    def _index_artifacts(self) -> Dict[str, Any]:
        """Size, digest and line count for every file, so a re-open can detect
        truncation or tampering."""
        index: Dict[str, Any] = {}
        for path in sorted(self._run_dir.rglob("*")):
            if not path.is_file() or path.name == "manifest.json":
                continue
            rel = str(path.relative_to(self._run_dir))
            payload = path.read_bytes()
            entry: Dict[str, Any] = {
                "byteCount": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "lineCount": None,
            }
            if rel.endswith(".jsonl"):
                entry["lineCount"] = sum(
                    1 for line in payload.decode("utf-8").splitlines() if line.strip())
            index[rel] = entry
        return index


__all__ = [
    "DISPOSITIONS", "GUI_VERSION", "ISSUE_KINDS", "MODES",
    "MODE_EVIDENCE_BASES", "REDACTED", "REQUIRED_ENTRIES", "SCHEMA_VERSION",
    "SOURCE_BOUNDARIES", "SOURCE_KINDS", "SUCCESS_DISPOSITIONS",
    "SessionStore", "SessionStoreError", "scrub_credentials",
]
