"""Replay adapter - live-O1 harness capture.

Owner: track **T3**.  Authority: ``docs/phase-b-gui/replay-sources.1.0.0.json``
(adapter ``lo1-capture``).

This module has two layers, and the split matters:

**Layer 1 - binding and resolution** (this section).  A capture is bound to the
schema *it names in its own* ``schemaVersion`` field and validated against it.
Artifacts are resolved through ``EVIDENCE-MANIFEST.files{}`` when a packaged
bundle is present, and by staged path otherwise.  Nothing here interprets
content; everything here can fail closed.

**Layer 2 - normalization** (``load_capture_run``).  Turns a bound document into
a SessionStore run directory.

Why binding is by declaration and not by shape
----------------------------------------------
Two capture schemas exist, ``1.0.0`` and ``2.0.0``, and their required top-level
key sets are identical - only the ``schemaVersion`` const differs.  An adapter
that pattern-matched the document would therefore accept a 3.0.0 capture as a
2.0.0 one and silently misread whatever changed.  The rule is: read the declared
version, look it up, refuse if it is not vendored.  An unknown version is a
``SCHEMA_MISMATCH`` naming both what was observed and what is supported - never
an alias, a coercion or a special case (phaseB_task.md constraints).

Why resolution goes through the manifest
----------------------------------------
The packaging step **renames** every staged artifact: ``hello-client.xml``
becomes ``client-hello.xml``, ``session-client-to-server.bin`` becomes
``client-to-server-wire.bin``, ``rpc-0005-...-reply.xml`` becomes
``reply-00.xml``, and the capture itself becomes ``evidence/capture.json``.  A
glob-based adapter silently loses files across the two layouts, so when a
manifest is present it is the only resolver.

Secret handling
---------------
The adapter is rooted at the capture directory and **never walks to its parent**:
the enclosing run directory holds ``pki/`` and ``secrets/`` material that is out
of scope and must not be read, copied, digested or displayed.  The capture itself
carries only opaque references (``secret://sftp`` and similar) and asserts
``secretValuesCaptured=false``; reference *names* may be shown and nothing else.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Final, List, Mapping, Optional, Tuple

#: ``schemaVersion`` -> vendored schema file under docs/upper-live-o1-harness/.
CAPTURE_SCHEMAS: Final[Dict[str, str]] = {
    "oran-aic-upper-live-o1-harness-capture/1.0.0": "capture-schema.1.0.0.json",
    "oran-aic-upper-live-o1-harness-capture/2.0.0": "capture-schema.2.0.0.json",
}

SCHEMA_DIR: Final[Path] = (
    Path(__file__).resolve().parents[4] / "docs" / "upper-live-o1-harness")

EVIDENCE_MANIFEST_NAME: Final[str] = "EVIDENCE-MANIFEST.json"

#: Layout the source was read from.  Recorded on the manifest source entry so a
#: reader can tell which resolver produced the artifact paths.
LAYOUT_STAGED: Final[str] = "STAGED_CAPTURE_DIRECTORY"
LAYOUT_BUNDLE: Final[str] = "PACKAGED_EVIDENCE_BUNDLE"

_SCHEMA_CACHE: Dict[str, Any] = {}


class CaptureError(Exception):
    """A capture that cannot be loaded, with the issue kind it maps to.

    ``kind`` is a ``dataIssues`` kind from the session-store schema, so a caller
    records the refusal in the run rather than discarding it.
    """

    def __init__(self, detail: str, *, kind: str = "SCHEMA_MISMATCH") -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


# --------------------------------------------------------------------------- #
# Schema binding
# --------------------------------------------------------------------------- #


def supported_schema_versions() -> Tuple[str, ...]:
    return tuple(sorted(CAPTURE_SCHEMAS))


def load_schema(version: str) -> Mapping[str, Any]:
    """Load the vendored schema for a declared capture version."""
    name = CAPTURE_SCHEMAS.get(version)
    if name is None:
        raise CaptureError(
            f"unsupported capture schemaVersion {version!r}; this build vendors "
            + ", ".join(supported_schema_versions()))
    if version not in _SCHEMA_CACHE:
        path = SCHEMA_DIR / name
        if not path.is_file():
            raise CaptureError(f"vendored capture schema is missing: {name}")
        _SCHEMA_CACHE[version] = json.loads(path.read_text(encoding="utf-8"))
    return _SCHEMA_CACHE[version]


def bind_schema(document: Mapping[str, Any]) -> Tuple[str, Mapping[str, Any]]:
    """Bind a capture to the schema it declares.

    Refuses a document with no ``schemaVersion``: with nothing to bind to, the
    only remaining option is to guess from the shape, and guessing is what this
    rule exists to prevent.
    """
    if not isinstance(document, Mapping):
        raise CaptureError("capture document is not a JSON object")
    version = document.get("schemaVersion")
    if not isinstance(version, str) or not version:
        raise CaptureError(
            "capture declares no schemaVersion; supported: "
            + ", ".join(supported_schema_versions()))
    return version, load_schema(version)


def validate_capture(document: Mapping[str, Any]) -> str:
    """Validate a capture against its declared schema; return the version.

    Uses the same Draft 2020-12 validator and format checker the O-RAN contract
    kernel uses, so a capture the release tooling accepts is a capture this
    console accepts - no second, looser opinion about validity.
    """
    version, schema = bind_schema(document)
    from jsonschema import Draft202012Validator, FormatChecker

    errors = sorted(Draft202012Validator(schema, format_checker=FormatChecker())
                    .iter_errors(document), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        pointer = "/" + "/".join(str(p) for p in first.path)
        raise CaptureError(
            f"capture does not satisfy {version} at {pointer}: {first.message} "
            f"({len(errors)} error(s))")
    return version


# --------------------------------------------------------------------------- #
# Artifact resolution
# --------------------------------------------------------------------------- #


class CaptureSource:
    """A bound, validated capture plus the resolver for its artifacts.

    ``root`` is the capture directory or the bundle directory.  Nothing outside
    it is ever read.
    """

    def __init__(self, root: Path, document: Mapping[str, Any], version: str,
                 *, layout: str, capture_path: Path,
                 manifest: Optional[Mapping[str, Any]] = None) -> None:
        self.root = root
        self.document = document
        self.schema_version = version
        self.layout = layout
        self.capture_path = capture_path
        self.manifest = manifest
        self._files: Dict[str, str] = {}
        self._missing: List[str] = []
        if manifest:
            self._files = self._build_rename_map(manifest)

    # -- construction ------------------------------------------------------- #

    @classmethod
    def open(cls, root: str | Path) -> "CaptureSource":
        """Open a staged capture directory or a packaged evidence bundle."""
        base = Path(root)
        if not base.is_dir():
            raise CaptureError(f"capture root is not a directory: {base}",
                               kind="CONNECTION_LOST")

        manifest_path = base / EVIDENCE_MANIFEST_NAME
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            rel = manifest.get("capturePath")
            if not isinstance(rel, str) or not rel:
                raise CaptureError("evidence manifest declares no capturePath")
            capture_path = _safe_join(base, rel)
            document = _read_json(capture_path)
            version = validate_capture(document)
            return cls(base, document, version, layout=LAYOUT_BUNDLE,
                       capture_path=capture_path, manifest=manifest)

        candidates = sorted(base.glob("capture*.json"))
        if not candidates:
            raise CaptureError(
                f"no capture document under {base.name}; expected capture*.json "
                f"or {EVIDENCE_MANIFEST_NAME}", kind="CONNECTION_LOST")
        if len(candidates) > 1:
            raise CaptureError(
                "capture directory holds more than one capture document: "
                + ", ".join(p.name for p in candidates))
        document = _read_json(candidates[0])
        version = validate_capture(document)
        return cls(base, document, version, layout=LAYOUT_STAGED,
                   capture_path=candidates[0])

    # -- resolution --------------------------------------------------------- #

    @staticmethod
    def _build_rename_map(manifest: Mapping[str, Any]) -> Dict[str, str]:
        """Map a staged capture-relative path onto its packaged member.

        The packaged member names are the *renamed* ones, so a staged reference
        such as ``raw/o1/pm-<digest>.xml`` has to be matched by digest, and a
        NETCONF rpc by its ordinal.  Where the manifest carries a digest, that is
        the match; nothing here globs on a filename, because the whole point of
        the rename is that filenames do not survive packaging.
        """
        by_digest: Dict[str, str] = {}
        for member, descriptor in (manifest.get("files") or {}).items():
            digest = (descriptor or {}).get("sha256")
            if isinstance(digest, str):
                by_digest.setdefault(digest, member)
        return by_digest

    def resolve(self, rel: str) -> Optional[Path]:
        """Resolve a capture-relative artifact reference to a readable file.

        Returns ``None`` when the artifact is not present.  A missing artifact is
        a ``DANGLING_REFERENCE`` for the caller to record, never a load failure:
        a capture whose large wire dumps were not packaged is still a capture.
        """
        if not rel:
            return None
        if self.layout == LAYOUT_STAGED:
            path = _safe_join(self.root, rel)
            return path if path.is_file() else None

        digest = self.digest_for(rel)
        if digest and digest in self._files:
            path = _safe_join(self.root, self._files[digest])
            if path.is_file():
                return path
        return None

    def digest_for(self, rel: str) -> Optional[str]:
        """The sha256 the capture itself records for a staged artifact path.

        Walks the capture for a ``{artifactPath, sha256}`` pair rather than
        trusting the filename, so bundle resolution is content-addressed.
        """
        if not hasattr(self, "_digest_index"):
            self._digest_index = _index_artifact_digests(self.document)
        return self._digest_index.get(rel)

    def read_bytes(self, rel: str) -> Optional[bytes]:
        path = self.resolve(rel)
        return path.read_bytes() if path is not None else None

    # -- identity ----------------------------------------------------------- #

    @property
    def run_id(self) -> str:
        return str(self.document.get("run", {}).get("runId", "unknown"))

    @property
    def capture_digest(self) -> str:
        return hashlib.sha256(self.capture_path.read_bytes()).hexdigest()

    def artifact_references(self) -> Dict[str, Optional[str]]:
        """Every staged artifact path the capture names, with its digest."""
        if not hasattr(self, "_digest_index"):
            self._digest_index = _index_artifact_digests(self.document)
        return dict(self._digest_index)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CaptureError(f"capture document is missing: {path.name}",
                           kind="CONNECTION_LOST") from exc
    except json.JSONDecodeError as exc:
        raise CaptureError(
            f"capture document is not valid JSON ({path.name}): {exc}",
            kind="DATA_TRUNCATED") from exc


def _safe_join(root: Path, rel: str) -> Path:
    """Join under ``root``, refusing anything that escapes it.

    The parent of a capture directory holds key and secret material.  A
    traversing reference is refused rather than clamped, because a capture that
    contains one is not a capture this console should be reading at all.
    """
    candidate = (root / rel).resolve()
    base = root.resolve()
    if candidate != base and base not in candidate.parents:
        raise CaptureError(
            f"artifact reference escapes the capture directory: {rel!r}")
    return candidate


#: The capture records an artifact's digest under one of several field names
#: depending on which section carries it: a raw-artifact node uses ``sha256``, a
#: retrieval uses ``byteSha256`` and a normalization record uses
#: ``sourceFileSha256``.  All three are the sha256 of the same bytes.
_DIGEST_KEYS: Final[tuple] = ("sha256", "byteSha256", "sourceFileSha256")


def _index_artifact_digests(node: Any,
                            found: Optional[Dict[str, Optional[str]]] = None
                            ) -> Dict[str, Optional[str]]:
    """Collect every artifact reference in the capture with its digest.

    The same path is referenced from several sections and only some of them
    carry the digest, so a later non-null digest upgrades an earlier null one.
    Locking in the first occurrence would leave the PM artifact - referenced
    first from a normalization record - permanently unresolvable in a packaged
    bundle, where resolution is content-addressed.
    """
    if found is None:
        found = {}
    if isinstance(node, Mapping):
        for key in ("artifactPath", "rawArtifactPath"):
            rel = node.get(key)
            if not isinstance(rel, str) or not rel:
                continue
            digest = next((node[k] for k in _DIGEST_KEYS
                           if isinstance(node.get(k), str)), None)
            if found.get(rel) is None:
                found[rel] = digest
        for value in node.values():
            _index_artifact_digests(value, found)
    elif isinstance(node, list):
        for value in node:
            _index_artifact_digests(value, found)
    return found


# --------------------------------------------------------------------------- #
# Layer 2 - normalization into a SessionStore run
# --------------------------------------------------------------------------- #

#: ``harnessOperations[].component`` -> timeline lane.
COMPONENT_LANE: Final[Dict[str, str]] = {
    "rapp": "COORDINATOR",
    "nonrt": "R1",
    "o1_consumer": "O1",
}

#: The capture's own id for this source.
SOURCE_ID: Final[str] = "lo1-capture"

#: Only COMPLETED maps to COMPLETED.  Every other declared disposition is a
#: FAILED run that keeps its data - a partial run is never a success.
_DISPOSITION_MAP: Final[Dict[str, str]] = {"COMPLETED": "COMPLETED"}


def _exchange_lane(endpoint_ref: Optional[str], uri: Optional[str]) -> str:
    """Lane for one HTTP exchange.

    Checked most specific first: an endpoint named ``r1DmePushDestination``
    contains both ``r1`` and ``dme``, and it belongs to the DME lane.
    """
    text = f"{endpoint_ref or ''} {uri or ''}".lower()
    if "dme" in text or "data-job" in text:
        return "DME"
    if "a1" in text:
        return "A1"
    if "r1" in text or "policies" in text:
        return "R1"
    return "R1"


def _declared_quality(record: Mapping[str, Any]) -> str:
    """The quality the capture declared, or an explicit admission that it did not.

    ``AMBIGUOUS`` rather than ``OK``: a record that states no quality has not
    been vouched for, and defaulting it to ``OK`` would let the absence of an
    assertion read as an assertion.
    """
    declared = record.get("quality")
    return str(declared) if declared else "AMBIGUOUS"


def _cell_scope(dn: str) -> str:
    """The cell identity out of a 3GPP distinguished name."""
    for part in str(dn).split(","):
        if part.strip().startswith("NRCellDU="):
            return part.strip()
    return dn


class _Timeline:
    """Merges the capture's event arrays into one ordered lane view.

    ``capture.run.orderingRule`` is ``MONOTONIC_OBSERVED_THEN_ARRAY_ORDER`` and
    all arrays share one ``0..sequenceHighWaterMark`` counter, so a single merge
    on ``(observed time, sequence, array order)`` is well defined.  Entries with
    no time of their own keep their array order behind the entry that produced
    them, and are emitted as ``DERIVED``.
    """

    def __init__(self) -> None:
        self._rows: List[Tuple[str, int, int, Dict[str, Any]]] = []
        self._order = 0

    def add(self, *, at: Optional[str], sequence: Optional[int],
            **event: Any) -> None:
        self._order += 1
        # A missing time sorts last rather than first: an event nobody stamped
        # is not evidence that it happened before everything else.
        self._rows.append((at or "9999", sequence if sequence is not None else 10**6,
                           self._order, event))

    def emit(self) -> List[Dict[str, Any]]:
        from gui.operator.store.records import timeline_event

        ordered = sorted(self._rows, key=lambda row: (row[0], row[1], row[2]))
        return [timeline_event(seq=index, **row[3])
                for index, row in enumerate(ordered)]


def build_timeline(document: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Every capture event array, merged into one operator timeline."""
    timeline = _Timeline()

    intent_op_at: Optional[str] = None
    intent_op_seq: Optional[int] = None
    for op in document.get("harnessOperations") or ():
        name = str(op.get("op") or "OPERATION")
        if name == "COORDINATOR_PROCESS_INTENT":
            intent_op_at = op.get("observedAt")
            intent_op_seq = op.get("sequence")
        timeline.add(
            at=op.get("observedAt"), sequence=op.get("sequence"),
            lane=COMPONENT_LANE.get(str(op.get("component")), "SESSION"),
            kind=name, t_utc=op.get("observedAt"),
            component=str(op.get("component") or ""),
            severity="INFO" if op.get("allowed", True) else "ERROR",
            title=f"{op.get('component')}: {name}",
            detail={"event": op.get("event"), "httpStatus": op.get("httpStatus"),
                    "argumentKeys": list(op.get("argumentKeys") or ()),
                    "outputKeys": list(op.get("outputKeys") or ()),
                    "sourceSequence": op.get("sequence")},
            ids={"correlationId": None})

    for exchange in document.get("exchanges") or ():
        response = exchange.get("response") or {}
        status = response.get("status")
        policy_id = response.get("locationLastPathSegment")
        timeline.add(
            at=exchange.get("observedAt"), sequence=exchange.get("sequence"),
            lane=_exchange_lane(exchange.get("endpointRef"),
                                exchange.get("resolvedUri")),
            kind=f"HTTP_{exchange.get('method', 'REQUEST')}",
            t_utc=exchange.get("observedAt"),
            component=str(exchange.get("peer") or ""),
            severity="INFO" if not exchange.get("timedOut") else "ERROR",
            title=f"{exchange.get('method')} {exchange.get('stepId') or ''} -> {status}",
            detail={"endpointRef": exchange.get("endpointRef"),
                    "status": status, "direction": exchange.get("direction"),
                    "requestBytes": (exchange.get("request") or {}).get("bodyByteCount"),
                    "responseBytes": response.get("bodyByteCount"),
                    "bodyAvailable": False, "gapId": "GAP-04",
                    "sourceSequence": exchange.get("sequence")},
            ids={"policyId": policy_id,
                 "correlationId": exchange.get("correlationId")})

    for rpc in (document.get("netconf") or {}).get("rpcs") or ():
        request = rpc.get("requestRaw") or {}
        timeline.add(
            at=rpc.get("at"), sequence=rpc.get("sequence"), lane="O1",
            kind="NETCONF_RPC", t_utc=rpc.get("at"), component="o1_provider",
            severity="INFO" if rpc.get("outcome") == "OK" else "ERROR",
            title=f"NETCONF {rpc.get('phase', '')} #{rpc.get('lifecycleOrdinal')}",
            detail={"outcome": rpc.get("outcome"), "phase": rpc.get("phase"),
                    "lifecycleOrdinal": rpc.get("lifecycleOrdinal"),
                    "sourceSequence": rpc.get("sequence")},
            source_ref=request.get("artifactPath"),
            ids={"correlationId": None})

    for notification in document.get("notifications") or ():
        files = notification.get("fileInfoList") or []
        timeline.add(
            at=notification.get("receivedAt"), sequence=notification.get("sequence"),
            lane="O1", kind="O1_FILE_READY", t_utc=notification.get("receivedAt"),
            component="o1_provider",
            title=f"file ready ({len(files)} file(s))",
            detail={"accepted": notification.get("accepted"),
                    "responseStatus": notification.get("responseStatus"),
                    "eventTime": notification.get("eventTime"),
                    "jobIds": [f.get("jobId") for f in files],
                    "sourceSequence": notification.get("sequence")},
            source_ref=(notification.get("raw") or {}).get("artifactPath"),
            ids={"correlationId": None})

    for retrieval in document.get("retrievals") or ():
        timeline.add(
            at=retrieval.get("completedAt"), sequence=retrieval.get("sequence"),
            lane="O1", kind="O1_FILE_RETRIEVED",
            t_utc=retrieval.get("completedAt"), component="o1_consumer",
            severity="INFO" if retrieval.get("outcome") == "RETRIEVED" else "ERROR",
            title=f"retrieved {retrieval.get('byteCount')} bytes",
            detail={"outcome": retrieval.get("outcome"),
                    "credentialRef": retrieval.get("credentialRef"),
                    "hostKeyVerified": retrieval.get("hostKeyVerified"),
                    "startedAt": retrieval.get("startedAt"),
                    "sourceSequence": retrieval.get("sequence")},
            source_ref=retrieval.get("rawArtifactPath"),
            ids={"correlationId": None})

    for state in ((document.get("netconf") or {}).get("readiness") or {}).get(
            "states") or ():
        timeline.add(
            at=state.get("at"), sequence=None, lane="SESSION",
            kind=f"READINESS_{state.get('state')}", t_utc=state.get("at"),
            component=str(state.get("owner") or ""),
            severity="INFO" if state.get("confirmed") else "WARNING",
            title=f"readiness {state.get('state')}",
            detail={"confirmed": state.get("confirmed"),
                    "initialStates": list(state.get("initialStates") or ()),
                    "postconditions": list(state.get("postconditions") or ())},
            ids={"correlationId": None})

    for action in (document.get("cleanup") or {}).get("actions") or ():
        timeline.add(
            at=action.get("at"), sequence=None, lane="TEARDOWN",
            kind=f"CLEANUP_{action.get('action')}", t_utc=action.get("at"),
            component=str(action.get("target") or ""),
            severity="INFO" if action.get("outcome") in ("DONE", "ALREADY_ABSENT")
            else "WARNING",
            title=f"{action.get('action')} {action.get('target')}",
            detail={"outcome": action.get("outcome"),
                    "detail": action.get("detail")},
            ids={"correlationId": None})

    # GAP-12: FSM transitions carry no timestamp.  They are emitted as DERIVED,
    # ordered immediately behind the operation that produced them, with tUtc
    # null so nothing can read an inferred order as a measured time.
    coordinator = document.get("coordinator") or {}
    for index, transition in enumerate(coordinator.get("fsmHistory") or ()):
        timeline.add(
            at=intent_op_at, sequence=intent_op_seq, lane="COORDINATOR",
            kind="FSM_TRANSITION", t_utc=None, origin="DERIVED",
            derivation="FSM transition order only; the capture carries no "
                       "per-transition timestamp (GAP-12)",
            component="coordinator",
            title=f"{transition.get('from')} -> {transition.get('to')}",
            detail={"from": transition.get("from"), "to": transition.get("to"),
                    "outcome": transition.get("outcome"),
                    "origin": transition.get("origin"), "index": index},
            ids={"correlationId": None})

    return timeline.emit()


def build_telemetry(document: Mapping[str, Any], *,
                    artifact_prefix: str) -> List[Dict[str, Any]]:
    """Every measurement the capture actually contains.

    Which is: two ``RRU.PrbDl`` scalars over one 60-second window for two cells,
    their ingest latencies, and the run-scoped policy and evidence counts from
    the before/after state.  The adapter does not pretend otherwise - it is an
    event and lifecycle record, not a KPI time series, and the chart renders
    those two points as annotated points with a sparse-data note rather than as
    a line.
    """
    from gui.operator.store.records import telemetry_sample

    samples: List[Dict[str, Any]] = []
    seq = 0
    for record in (document.get("normalization") or {}).get("records") or ():
        dn = str(record.get("measuredObjectDn", ""))
        scope_id = _cell_scope(dn)
        artifact = record.get("rawArtifactPath")
        samples.append(telemetry_sample(
            seq=seq, metric=str(record.get("measurementName")),
            value=record.get("value"), unit=str(record.get("unit", "")),
            scope_level="CELL", scope_id=scope_id, scope_dn=dn,
            boundary="O1_ASSURANCE", interface="O1",
            management_service="Performance Assurance",
            t_utc=record.get("observedAt"),
            artifact_ref=f"{artifact_prefix}/{artifact}" if artifact else None,
            observation_id=record.get("recordJcsSha256"),
            window=record.get("window"),
            aggregation=record.get("aggregation"),
            age_ms=record.get("measurementAgeMs"),
            quality=_declared_quality(record)))
        seq += 1

        if record.get("ingestLatencyMs") is not None:
            samples.append(telemetry_sample(
                seq=seq, metric="o1_ingest_latency_ms",
                value=float(record["ingestLatencyMs"]), unit="ms",
                scope_level="CELL", scope_id=scope_id, scope_dn=dn,
                boundary="O1_ASSURANCE", interface="O1",
                management_service="Performance Assurance",
                t_utc=record.get("retrievedAt") or record.get("observedAt"),
                observation_id=record.get("recordJcsSha256"),
                quality=_declared_quality(record)))
            seq += 1

    # Run-scoped counts, before and after, so the results view can show what the
    # run changed rather than only where it ended.
    state = document.get("state") or {}
    run = document.get("run") or {}
    for phase, at in (("before", run.get("startedAt")), ("after", run.get("endedAt"))):
        block = state.get(phase) or {}
        for metric, key, boundary in (
                ("policy_count", "policyCount", "RAPP_INTERNAL"),
                ("evidence_record_count", "evidenceCommitCount", "R1_DME")):
            if block.get(key) is None:
                continue
            samples.append(telemetry_sample(
                seq=seq, metric=metric, value=float(block[key]), unit="count",
                scope_level="RUN", scope_id=str(run.get("runId", "run")),
                boundary=boundary,
                management_service=("R1 policy management state snapshot"
                                    if metric == "policy_count"
                                    else "R1 DME evidence ledger"),
                # a state counter read out of the capture's own before/after
                # block: the capture asserts it, so OK is a statement, not a
                # default
                t_utc=at, quality="OK"))
            seq += 1
    return samples


def build_episode(document: Mapping[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """The single coordinator episode a capture records, plus its one cycle.

    A capture has no confidence, no theta*, no per-stage latency and no LLM call
    record.  Those fields are written as ``null`` rather than omitted, so the
    Decision workspace renders "Unavailable" in a populated row instead of an
    empty panel that looks like a rendering bug.
    """
    from gui.operator.store.records import cycle_trace, episode_trace

    coordinator = document.get("coordinator") or {}
    run = document.get("run") or {}
    history = list(coordinator.get("fsmHistory") or ())
    path: List[str] = []
    if history:
        path.append(str(history[0].get("from")))
        path.extend(str(t.get("to")) for t in history)

    episode_id = next(
        (ref.split(":", 1)[1] for ref in coordinator.get("ledgerReferences") or ()
         if str(ref).startswith("episode:")),
        f"{run.get('runId', 'capture')}-episode-0")

    policy_ids = list((document.get("state") or {}).get("after", {})
                      .get("policyIds") or ())

    episode = episode_trace(
        episode_id=episode_id,
        # A capture carries the coordinator's execution, not the operator's
        # words.  An empty intent text is the honest reading, and a data issue
        # records why it is empty rather than leaving a reader to guess.
        intent_text="",
        terminal_outcome=coordinator.get("terminalOutcome"),
        runId=run.get("runId"),
        startedAt=run.get("startedAt"), endedAt=run.get("endedAt"),
        fsmPath=path,
        terminalReason=None,
        success=coordinator.get("terminalOutcome") in
        ("commit_original", "commit_revised"),
        latencyMs=None, parseMs=None, schemaValid=None,
        negotiationRounds=None, rolledBack=None,
        policyIds=policy_ids,
        evidenceRecordId=coordinator.get("terminalEvidenceRef"),
        executionMode=coordinator.get("executionMode"),
        ledgerReferences=list(coordinator.get("ledgerReferences") or ()),
        nowInput=coordinator.get("nowInput"))
    cycle = cycle_trace(
        episode_id=episode_id, cycle_index=0,
        feasible=None, rawConfidence=None, calibratedProbability=None,
        threshold=None, calibrationReason=None, routedTo=None,
        alternatives=[], agreement=None, reasoningSummary=None,
        latencies={"inferenceMs": None, "schemaAdmissionMs": None,
                   "executorWriteMs": None, "readbackMs": None,
                   "validationMs": None, "negotiationMs": None,
                   "rollbackMs": None, "recoveryMs": None},
        unavailableReason="a live-O1 capture records the coordinator's "
                          "execution, not its decision inputs")
    return episode, cycle


def build_readiness(document: Mapping[str, Any]) -> Dict[str, Any]:
    """The five-segment intent -> policy -> evidence readiness ladder."""
    readiness = ((document.get("netconf") or {}).get("readiness") or {})
    segments = []
    for state in readiness.get("states") or ():
        segments.append({
            "segmentId": state.get("state"),
            "confirmed": bool(state.get("confirmed")),
            "at": state.get("at"),
            "owner": state.get("owner"),
            "initialStates": list(state.get("initialStates") or ()),
            "postconditions": list(state.get("postconditions") or ()),
        })
    return {
        "segments": segments,
        "complete": bool(readiness.get("complete")),
        "unmet": list(readiness.get("unmet") or ()),
        "requiredInitialStates": list(readiness.get("requiredInitialStates") or ()),
        "teardownOrder": list(readiness.get("teardownOrder") or ()),
        "orderSource": readiness.get("orderSource"),
    }


def build_provenance(document: Mapping[str, Any]) -> Dict[str, Any]:
    """The read-only provenance panel: gates, identity and external calls.

    ``externalCalls`` is worth surfacing because it is the machine-checked form
    of the same boundary this console obeys.
    """
    authority = document.get("authority") or {}
    external = document.get("externalCalls") or {}
    redaction = document.get("redaction") or {}
    return {
        "gateResult": authority.get("gateResult"),
        "checks": [{"id": c.get("id"), "outcome": c.get("outcome")}
                   for c in authority.get("checks") or ()],
        "scope": authority.get("scope"),
        "selfIssued": authority.get("selfIssued"),
        "provider": document.get("provider"),
        "contract": document.get("contract"),
        "revisions": document.get("revisions"),
        "deployment": {k: v for k, v in (document.get("deployment") or {}).items()
                       if k != "resolvedRoots"},
        "profile": document.get("profile"),
        "externalCalls": {
            "hardwareCalls": external.get("hardwareCalls"),
            "externalLiveTargetCalls": external.get("externalLiveTargetCalls"),
            "forbiddenEgressAttempts": external.get("forbiddenEgressAttempts"),
            "guardInstalled": external.get("guardInstalled"),
            "violations": external.get("violations"),
        },
        # reference NAMES only; the capture asserts no value was ever captured
        "secretReferencesObserved": list(
            redaction.get("secretReferencesObserved") or ()),
        "secretValuesCaptured": redaction.get("secretValuesCaptured"),
        "credentialMaterialCaptured": redaction.get("credentialMaterialCaptured"),
        "appliedRules": [r.get("ruleId") for r in document.get("appliedRules") or ()],
    }


def load_capture_run(source: "CaptureSource | str | Path", runs_root,
                     *, capability_manifest: Optional[Mapping[str, Any]] = None
                     ) -> Any:
    """Normalize a capture into a finalized SessionStore run.

    Returns the store, finalized.  ``manifest.mode`` is ``REPLAY`` and its
    evidence is ``REPLAY_OF_RECORDED_SOURCE`` with the capture's digest - there
    is no argument, and no UI control, that makes this LIVE.
    """
    from gui.operator.store.metric_index import MetricIndexBuilder
    from gui.operator.store.session_store import SessionStore

    if not isinstance(source, CaptureSource):
        source = CaptureSource.open(source)
    document = source.document
    run = document.get("run") or {}

    store = SessionStore.create(
        runs_root, mode="REPLAY",
        mode_evidence={"basis": "REPLAY_OF_RECORDED_SOURCE",
                       "sourceRunIds": [source.run_id],
                       "sourceDigests": [source.capture_digest]},
        profile_id=(document.get("profile") or {}).get("executionProfile"),
        config_snapshot={
            "source": {"adapter": SOURCE_ID, "layout": source.layout,
                       "schemaVersion": source.schema_version},
            "profile": document.get("profile"),
            "scenario": document.get("scenario"),
            "contract": document.get("contract"),
            "deployment": document.get("deployment"),
            "clock": document.get("clock"),
            "capabilityManifest": capability_manifest,
        })
    try:
        store.add_source(
            source_id=SOURCE_ID,
            kind=("LO1_EVIDENCE_BUNDLE" if source.layout == LAYOUT_BUNDLE
                  else "LO1_CAPTURE"),
            boundary="O1_ASSURANCE", path=str(source.root),
            digest=source.capture_digest, schema_version=source.schema_version,
            adapter="gui/operator/sources/adapters/lo1_capture.py",
            notes=f"layout {source.layout}")
        store.set_contract(
            authority_version=(document.get("contract") or {}).get("contractProfile"),
            bundle_digest=(document.get("contract") or {})
            .get("bundleManifestSha256"))

        # -- raw artifacts, byte-preserved --------------------------------- #
        prefix = f"raw/{SOURCE_ID}"
        store.copy_raw(SOURCE_ID, source.capture_path, "capture.json")
        for rel in sorted(source.artifact_references()):
            path = source.resolve(rel)
            if path is None:
                store.record_issue(
                    "DANGLING_REFERENCE",
                    f"the capture references {rel} but it is not present in this "
                    f"{source.layout.lower().replace('_', ' ')}",
                    gap_id="GAP-14")
                continue
            store.copy_raw(SOURCE_ID, path, rel)

        # state/ is removed at teardown by design, so durableStatePath dangles
        for state in ((document.get("netconf") or {}).get("readiness") or {}).get(
                "states") or ():
            durable = (state.get("evidence") or {}).get("durableStatePath")
            if durable and not (source.root / durable).is_file():
                store.record_issue(
                    "DANGLING_REFERENCE",
                    f"durableStatePath {durable} was removed at teardown "
                    "(cleanup REMOVE TEMP_STATE / DELETE O1_SUBSCRIPTION); the "
                    "drill-down renders Not applicable with that reason",
                    at=state.get("at"), gap_id="GAP-14")

        # -- events, telemetry, decision ------------------------------------ #
        for event in build_timeline(document):
            store.append_event(event)

        samples = build_telemetry(document, artifact_prefix=prefix)
        for sample in samples:
            store.append_telemetry(sample)

        episode, cycle = build_episode(document)
        store.append_episode(episode)
        store.append_cycle(cycle)

        # -- honest absences ------------------------------------------------ #
        store.record_issue(
            "UNSUPPORTED_FIELD",
            "exchanges[] carry byte counts and digests but no HTTP bodies, so "
            "the A1 policy object, the DME job definition and the status payload "
            "cannot be rendered", gap_id="GAP-04")
        store.record_issue(
            "UNSUPPORTED_FIELD",
            "coordinator.fsmHistory[] carries no per-transition timestamp; the "
            "transitions are emitted with origin DERIVED and no observation time",
            gap_id="GAP-12")
        store.record_issue(
            "MISSING_METRIC",
            "the capture records the coordinator's execution but not the "
            "operator's intent text, confidence, theta* or per-stage latency",
            gap_id="GAP-03")

        builder = MetricIndexBuilder(source_id=SOURCE_ID,
                                     capability_manifest=capability_manifest)
        builder.observe_all(samples)
        store.write_metric_index(builder.build())

        summary = {
            "runId": store.run_id,
            "sourceRunId": source.run_id,
            "mode": store.mode,
            "startedAt": run.get("startedAt"),
            "endedAt": run.get("endedAt"),
            "outcomes": {episode["eq12State"]: 1} if episode["eq12State"] else {},
            "terminalOutcomes": {episode["terminalOutcome"]: 1}
            if episode["terminalOutcome"] else {},
            "readiness": build_readiness(document),
            "provenance": build_provenance(document),
            "stateBeforeAfter": {"before": (document.get("state") or {}).get("before"),
                                 "after": (document.get("state") or {}).get("after")},
            "dataIssues": store.data_issues(),
        }
        store.write_summary(summary)

        disposition = _DISPOSITION_MAP.get(str(run.get("disposition")), "FAILED")
        if disposition != "COMPLETED":
            store.record_issue(
                "PARTIAL_DATA",
                f"capture run.disposition is {run.get('disposition')!r}; the run "
                "is recorded as FAILED and is never summarized as success")
        store.finalize(disposition)
        return store
    except Exception:
        store.finalize("FAILED")
        raise


__all__ = [
    "CAPTURE_SCHEMAS", "COMPONENT_LANE", "CaptureError", "CaptureSource",
    "EVIDENCE_MANIFEST_NAME", "LAYOUT_BUNDLE", "LAYOUT_STAGED", "SOURCE_ID",
    "bind_schema", "build_episode", "build_provenance", "build_readiness",
    "build_telemetry", "build_timeline", "load_capture_run", "load_schema",
    "supported_schema_versions", "validate_capture",
]
