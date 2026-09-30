"""Phase B Operator Console - Replay source façade.

Owner: track **T3**.  Authority: ``docs/phase-b-gui/replay-sources.1.0.0.json``.

One entry point for both adapters, so a caller loads a recorded source without
knowing which adapter reads it, and so the mode can never be passed in from the
outside.

The façade exists for a specific reason: the *only* thing a workspace should
need to know about Replay is that it produces a SessionStore run directory
identical in shape to a live one.  Everything source-specific - the capture
schema binding, the manifest-driven artifact resolution, the flat
``experiment_results`` layout - stays behind this line.

What this deliberately does **not** offer is a mode argument.  ``manifest.mode``
is set by the adapter from source evidence, and there is no parameter, no
setting and no UI control anywhere in the console that overrides it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Final, List, Mapping, Optional

from gui.operator.sources.adapters import (
    EXPERIMENT_RUN, LIVECONSOLE_RUN, LO1_CAPTURE)
from gui.operator.sources.adapters import experiments_run as _experiments
from gui.operator.sources.adapters import liveconsole_run as _liveconsole
from gui.operator.sources.adapters import lo1_capture as _capture


class ReplayError(Exception):
    """A recorded source that cannot be loaded, with its data-issue kind."""

    def __init__(self, detail: str, *, kind: str = "SCHEMA_MISMATCH") -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


#: What each adapter can and cannot show, for the Settings/Integration panel.
#: Written out rather than discovered, because "what this source cannot show" is
#: exactly the thing an operator needs before choosing it, and it is not
#: derivable from a directory listing.
SOURCE_DESCRIPTIONS: Final[Dict[str, Dict[str, Any]]] = {
    LO1_CAPTURE: {
        "title": "live-O1 harness capture",
        "mode": "REPLAY",
        "carries": ["sequenced lifecycle and exchange events", "FSM path",
                    "five-segment readiness ladder", "gate and provenance record",
                    "two RRU.PrbDl scalars over one 60 s window"],
        "cannotShow": ["a KPI time series (two scalars is not a series)",
                       "HTTP bodies (GAP-04)",
                       "confidence, theta* or per-stage decision latency",
                       "RSRP, SINR, MCS, handover or IQ"],
    },
    LIVECONSOLE_RUN: {
        "title": "LIVE console evidence (Kernel event stream)",
        "mode": "REPLAY",
        "carries": ["the Kernel event stream, replayed through the same reducer",
                    "the decision axes and the settlement, re-derived from the "
                    "reduced state rather than read out of the run record",
                    "the gateway operation sequence and the harm ledger",
                    "the counter samples the objective's contracts bound",
                    "the supplementary half from a 1.1.0 run: each cap's "
                    "policy, the UE it was entitled to read back, and whether "
                    "the binding reached RESTORED"],
        "cannotShow": ["any KPI registry metric - throughput, RSRP, SINR, MCS, "
                       "PRB utilization: an assurance stream carries the "
                       "counters its contracts bound and nothing else",
                       "an Eq.12 state (the Kernel's six case terminations are "
                       "not the coordinator's three)",
                       "a run whose stream does not reproduce its recorded "
                       "terminal state hash - that is refused, not rendered",
                       "the supplementary half of a 1.0.0 run: it predates the "
                       "record, and the absence is shown as an absence of "
                       "record rather than as 'no cap'"],
    },
    EXPERIMENT_RUN: {
        "title": "Offline experiments-runner output",
        "mode": "from _meta.mode",
        "carries": ["per-UE throughput and radio time series",
                    "the full EpisodeRecord decision and latency decomposition",
                    "phase boundaries"],
        "cannotShow": ["anything that arrived over an O-RAN boundary - the "
                       "source class is EXPERIMENT_RECORD even for a live run",
                       "A1 policy or DME evidence state",
                       "an absolute wall clock (StepRecord carries elapsed "
                       "seconds only)"],
    },
}


def detect_adapter(path) -> Optional[str]:
    """Which adapter reads this directory, or ``None``.

    Detection is by *layout*, which is legitimate here - it decides which reader
    to hand the directory to, not how to interpret its contents.  The capture's
    own ``schemaVersion`` still decides how it is read.
    """
    root = Path(path)
    if not root.is_dir():
        return None
    if (root / _capture.EVIDENCE_MANIFEST_NAME).is_file():
        return LO1_CAPTURE
    if any(root.glob("capture*.json")):
        return LO1_CAPTURE
    if _liveconsole.list_sessions(root):
        return LIVECONSOLE_RUN
    if any(root.glob("*_metrics.json")):
        return EXPERIMENT_RUN
    return None


def list_sessions(path) -> List[str]:
    """Selectable sessions in a source directory.

    A capture directory holds exactly one run; an ``experiment_results``
    directory is flat and holds many, discriminated by ``session_id``.
    """
    adapter = detect_adapter(path)
    if adapter == EXPERIMENT_RUN:
        return _experiments.list_sessions(path)
    if adapter == LIVECONSOLE_RUN:
        return _liveconsole.list_sessions(path)
    if adapter == LO1_CAPTURE:
        return [_capture.CaptureSource.open(path).run_id]
    return []


def load(path, runs_root, *, session_id: Optional[str] = None,
         capability_manifest: Optional[Mapping[str, Any]] = None) -> Any:
    """Load a recorded source into a finalized SessionStore run.

    Raises :class:`ReplayError` with the data-issue kind the caller should
    record.  A refusal is never silent and never partial: either a run directory
    exists and is finalized, or the caller is told why not.
    """
    adapter = detect_adapter(path)
    if adapter is None:
        raise ReplayError(
            f"{Path(path)} holds neither a capture document nor an "
            "experiments-runner output", kind="CONNECTION_LOST")
    try:
        if adapter == LO1_CAPTURE:
            return _capture.load_capture_run(
                path, runs_root, capability_manifest=capability_manifest)
        if adapter == LIVECONSOLE_RUN:
            return _liveconsole.load_liveconsole_run(
                path, runs_root, run_id=session_id,
                capability_manifest=capability_manifest)
        sessions = _experiments.list_sessions(path)
        chosen = session_id or (sessions[0] if sessions else None)
        if chosen is None:
            raise ReplayError(f"no runner session under {Path(path)}",
                              kind="CONNECTION_LOST")
        return _experiments.load_experiment_run(
            path, chosen, runs_root, capability_manifest=capability_manifest)
    except (_capture.CaptureError, _experiments.ExperimentRunError,
            _liveconsole.LiveConsoleRunError) as exc:
        raise ReplayError(exc.detail, kind=exc.kind) from exc


__all__ = [
    "EXPERIMENT_RUN", "LIVECONSOLE_RUN", "LO1_CAPTURE", "ReplayError",
    "SOURCE_DESCRIPTIONS", "detect_adapter", "list_sessions", "load",
]
