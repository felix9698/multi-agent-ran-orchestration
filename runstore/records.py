"""Record builders for the SessionStore.

Owner: track **T3**.

Neutral by location as well as by dependency: it lives at the top level so
``assurance/batch/`` can share the one run-directory schema without importing a
console (Gate 2).  ``gui/operator/store/records.py`` re-exports it unchanged.

Schema authority: ``docs/phase-b-gui/session-store.1.0.0.schema.json``.

Every line written into a run directory is built here.  Putting the constructors
in one place is what makes the honesty rules *mechanical* instead of a habit each
adapter has to remember:

* ``value=None`` means honest-unknown.  It is written as JSON ``null``, its
  quality is forced away from ``OK``, and it is never coerced to ``0`` and never
  interpolated.  The distinction between "zero" and "not measured" is load
  bearing in this system and the schema exists to keep it.
* A measurement keeps the observation time its source declared.  Nothing is
  re-stamped to "now"; a source that carries only ordering gets ``t_rel_s`` and
  ``origin=DERIVED`` with the derivation written out.
* A metric-index entry whose status is not ``OK`` **must** carry a reason.  The
  builder raises rather than emitting a statusless absence, because an
  unexplained ``UNAVAILABLE`` teaches an operator nothing.
* an ``UNSUPPORTED`` metric carries ``sampleCount == 0``, and an ``UNAVAILABLE``
  one may carry rows but never an ``OK``-quality one: a metric with no source
  cannot have data, and a metric that answered with nothing cannot have a good
  sample.  Either would mean a different measurement had been substituted.
* An LLM call record is content-free by construction - model identity, latency,
  prompt hash and schema validity only.  Prompt text, response text and any
  ``reasoning_content`` cannot be placed in it.

This module is stdlib-only and imports no toolkit: it is used by the headless
export path and by hermetic tests that run without a display.
"""

from __future__ import annotations

from typing import Any, Dict, Final, Mapping, Optional, Sequence

# --------------------------------------------------------------------------- #
# Vocabularies (mirrors of the schema enums, so a typo fails fast and locally)
# --------------------------------------------------------------------------- #

SCOPE_LEVELS: Final[tuple] = ("CELL", "UE", "SLICE", "NODE", "RUN")

SOURCE_BOUNDARIES: Final[tuple] = (
    "R1_DME", "O1_ASSURANCE", "EXPERIMENT_RECORD", "RAPP_INTERNAL",
)

QUALITIES: Final[tuple] = (
    "OK", "SUSPECT", "STALE", "MISSING", "NOT_AVAILABLE", "AMBIGUOUS",
)

LANES: Final[tuple] = (
    "INTENT", "DECISION", "COORDINATOR", "R1", "A1", "O1", "DME",
    "LOWER_FEEDBACK", "SESSION", "WARNING", "TEARDOWN",
)

SEVERITIES: Final[tuple] = ("INFO", "WARNING", "ERROR")

ORIGINS: Final[tuple] = ("OBSERVED", "DERIVED")

#: Event kinds that may appear as a chart annotation (schema ``annotatable``).
ANNOTATABLE_KINDS: Final[tuple] = (
    "INTENT_SUBMITTED", "DECISION_COMPLETE", "POLICY_APPLIED",
    "EVIDENCE_RECEIVED", "NEGOTIATION_ROUND", "ROLLBACK", "SESSION_ENDED",
)

#: Metric-index statuses.  A subset of the GUI status vocabulary: a *metric*
#: cannot be BLOCKED, EXTERNAL or LAB_HARDWARE - those describe elements.
METRIC_STATUSES: Final[tuple] = (
    "OK", "DEGRADED", "STALE", "UNKNOWN", "UNAVAILABLE", "UNSUPPORTED",
    "NOT_APPLICABLE",
)

#: Statuses that assert this deployment has no source for the metric at all.  A
#: sample under one of these would mean a value was substituted from somewhere
#: else, which is the exact failure the metric registry exists to prevent.
#: ``UNAVAILABLE`` is deliberately NOT in this set: it means a source exists and
#: delivered nothing *usable*, which is compatible with rows that exist and carry
#: only nulls.  That case is constrained separately, below.
NO_SAMPLE_STATUSES: Final[tuple] = ("UNSUPPORTED", "NOT_APPLICABLE")

LLM_STAGES: Final[tuple] = ("PARSE", "FEASIBILITY", "ALTERNATIVES")

#: Fields an LLM call record may never carry, whatever the caller intends.
FORBIDDEN_LLM_FIELDS: Final[tuple] = (
    "prompt", "prompt_text", "promptText", "response", "response_text",
    "responseText", "completion", "reasoning", "reasoning_content",
    "reasoningContent", "thinking", "messages", "content",
)

EQ12_STATES: Final[tuple] = ("Admitted", "NotAdmitted", "TechnicalFailsafe")

TERMINAL_OUTCOMES: Final[tuple] = (
    "commit_original", "commit_revised", "pending_not_admitted",
    "technical_failsafe",
)

TERMINAL_OUTCOME_TO_EQ12: Final[Dict[str, str]] = {
    "commit_original": "Admitted",
    "commit_revised": "Admitted",
    "pending_not_admitted": "NotAdmitted",
    "technical_failsafe": "TechnicalFailsafe",
}


class RecordError(ValueError):
    """A record that would violate a schema or an honesty rule."""


def _finite(value: Any) -> bool:
    """Finite, numeric and not a bool.  Mirrors ``experiments/metrics.py``."""
    if value is None or isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return value == value and value not in (float("inf"), float("-inf"))


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RecordError(message)


# --------------------------------------------------------------------------- #
# Telemetry
# --------------------------------------------------------------------------- #


def telemetry_sample(*, seq: int, metric: str, value: Optional[float], unit: str,
                     scope_level: str, scope_id: str,
                     boundary: str,
                     t_utc: Optional[str] = None,
                     t_rel_s: Optional[float] = None,
                     scope_dn: Optional[str] = None,
                     interface: Optional[str] = None,
                     management_service: Optional[str] = None,
                     artifact_ref: Optional[str] = None,
                     observation_id: Optional[str] = None,
                     window: Optional[Mapping[str, str]] = None,
                     aggregation: Optional[str] = None,
                     sampling_interval_ms: Optional[int] = None,
                     age_ms: Optional[int] = None,
                     quality: str,
                     derived: bool = False,
                     derivation: Optional[str] = None) -> Dict[str, Any]:
    """Build one ``normalized/telemetry.jsonl`` line.

    ``value=None`` is the honest unknown and is preserved.  A caller that passes
    ``None`` with ``quality='OK'`` is corrected to ``MISSING`` rather than
    accepted: "we have no value and everything is fine" is not a state this
    system can be in, and letting it through is how a gap becomes a zero three
    layers later.

    ``quality`` is **required**.  It used to default to ``OK``, which meant a
    caller that simply did not know silently asserted that the source had
    vouched for the value - and an exported sample then read as a sound
    measurement on the strength of a default.  Every caller now states it, and a
    source that declares no quality of its own says ``AMBIGUOUS`` rather than
    letting the parameter list decide.
    """
    _require(isinstance(seq, int) and seq >= 0, "seq must be a non-negative int")
    _require(bool(metric), "metric id is required")
    _require(scope_level in SCOPE_LEVELS, f"unknown scope level {scope_level!r}")
    _require(boundary in SOURCE_BOUNDARIES, f"unknown boundary {boundary!r}")
    _require(quality in QUALITIES, f"unknown quality {quality!r}")
    _require(value is None or _finite(value),
             f"{metric}: a non-finite value is not a measurement")
    _require(t_utc is not None or t_rel_s is not None,
             f"{metric}: a sample needs an observation time or a relative time")
    if derived:
        _require(bool(derivation),
                 f"{metric}: a derived sample must state its derivation")

    if value is None and quality == "OK":
        quality = "MISSING"

    sample: Dict[str, Any] = {
        "seq": seq,
        "tUtc": t_utc,
        "tRelS": t_rel_s,
        "metric": metric,
        "value": value,
        "unit": unit,
        "scope": {"level": scope_level, "id": scope_id, "dn": scope_dn},
        "source": {
            "boundary": boundary,
            "interface": interface,
            "managementService": management_service,
            "artifactRef": artifact_ref,
            "observationId": observation_id,
        },
        "window": dict(window) if window else None,
        "aggregation": aggregation,
        "samplingIntervalMs": sampling_interval_ms,
        "ageMs": age_ms,
        "quality": quality,
        "derived": bool(derived),
        "derivation": derivation,
    }
    return sample


# --------------------------------------------------------------------------- #
# Timeline
# --------------------------------------------------------------------------- #


def timeline_event(*, seq: int, lane: str, kind: str,
                   severity: str = "INFO", origin: str = "OBSERVED",
                   t_utc: Optional[str] = None,
                   t_mono_ns: Optional[int] = None,
                   t_rel_s: Optional[float] = None,
                   title: str = "", component: Optional[str] = None,
                   derivation: Optional[str] = None,
                   ids: Optional[Mapping[str, Optional[str]]] = None,
                   detail: Optional[Mapping[str, Any]] = None,
                   source_ref: Optional[str] = None,
                   annotatable: Optional[bool] = None) -> Dict[str, Any]:
    """Build one ``events/timeline.jsonl`` line.

    A ``DERIVED`` event must say what it was derived from - it renders on its own
    track precisely so it is never read as a measured time, and the derivation is
    what justifies the track.

    An ``ERROR`` event must carry at least one correlation id.  phaseB_task.md
    section 2 requires an important error to name its intent, policy, run,
    component and time; an error with nothing to correlate on is an error an
    operator cannot act on.
    """
    _require(isinstance(seq, int) and seq >= 0, "seq must be a non-negative int")
    _require(lane in LANES, f"unknown lane {lane!r}")
    _require(bool(kind), "event kind is required")
    _require(severity in SEVERITIES, f"unknown severity {severity!r}")
    _require(origin in ORIGINS, f"unknown origin {origin!r}")
    if origin == "DERIVED":
        _require(bool(derivation), f"{kind}: a derived event must state its derivation")

    id_block: Dict[str, Any] = {
        "runId": None, "intentId": None, "intentRevision": None,
        "policyId": None, "policyRevision": None, "episodeId": None,
        "cycleId": None, "evidenceId": None, "observationId": None,
        "correlationId": None,
    }
    for key, value in (ids or {}).items():
        _require(key in id_block, f"unknown correlation key {key!r}")
        id_block[key] = value

    if severity == "ERROR":
        _require(any(v is not None for v in id_block.values()),
                 f"{kind}: an error event must carry at least one correlation id")

    if annotatable is None:
        annotatable = kind in ANNOTATABLE_KINDS

    return {
        "seq": seq,
        "tUtc": t_utc,
        "tMonoNs": t_mono_ns,
        "tRelS": t_rel_s,
        "lane": lane,
        "kind": kind,
        "severity": severity,
        "origin": origin,
        "derivation": derivation,
        "component": component,
        "ids": id_block,
        "title": (title or kind)[:200],
        "detail": dict(detail) if detail else None,
        "sourceRef": source_ref,
        "annotatable": bool(annotatable),
    }


# --------------------------------------------------------------------------- #
# Decision records
# --------------------------------------------------------------------------- #


def episode_trace(*, episode_id: str, intent_text: str,
                  terminal_outcome: Optional[str],
                  **fields: Any) -> Dict[str, Any]:
    """Build one ``decision/episodes.jsonl`` line.

    ``eq12State`` is derived from the four-way outcome through the *same* table
    the research console uses, so the two consoles cannot disagree.  An outcome
    outside the vocabulary is refused rather than mapped to a plausible
    neighbour.

    ``hasConflict`` defaults to ``None``.  GAP-03: the coordinator does not
    persist the S1 screening verdict, so there is nothing to read - and inferring
    "no conflict" from its absence would be a fabricated finding.
    """
    _require(bool(episode_id), "episodeId is required")
    if terminal_outcome is None:
        eq12 = None
    else:
        _require(terminal_outcome in TERMINAL_OUTCOMES,
                 f"unknown terminal outcome {terminal_outcome!r}")
        eq12 = TERMINAL_OUTCOME_TO_EQ12[terminal_outcome]

    trace: Dict[str, Any] = {
        "episodeId": episode_id,
        "intentText": intent_text,
        "terminalOutcome": terminal_outcome,
        "eq12State": eq12,
        "hasConflict": None,
    }
    trace.update(fields)
    return trace


def cycle_trace(*, episode_id: str, cycle_index: int,
                **fields: Any) -> Dict[str, Any]:
    """Build one ``decision/cycles.jsonl`` line.

    ``thresholdAppliedTo`` defaults to ``calibrated_probability`` because that is
    what the engine routes on.  It is written into every record so the operator
    can *see* that routing is not on the raw score, rather than being told.
    """
    _require(bool(episode_id), "episodeId is required")
    _require(isinstance(cycle_index, int) and cycle_index >= 0,
             "cycleIndex must be a non-negative int")
    trace: Dict[str, Any] = {
        "episodeId": episode_id,
        "cycleIndex": cycle_index,
        "thresholdAppliedTo": "calibrated_probability",
    }
    trace.update(fields)
    return trace


def llm_call_trace(*, seq: int, stage: str, backend_name: str,
                   **fields: Any) -> Dict[str, Any]:
    """Build one ``decision/llm-calls.jsonl`` line.

    Deliberately content-free.  ``decision/llm_backend.py`` copies a thinking
    model's ``reasoning_content`` into the response when the visible answer is
    empty, and that text can reach a result object through the schema-rejection
    paths.  The engine itself keeps only ``prompt_hash``; this record does not
    widen that, and a caller that tries to attach text is refused rather than
    quietly trimmed.
    """
    _require(isinstance(seq, int) and seq >= 0, "seq must be a non-negative int")
    _require(stage in LLM_STAGES, f"unknown LLM stage {stage!r}")
    _require(bool(backend_name), "backendName is required")
    for name in fields:
        _require(name not in FORBIDDEN_LLM_FIELDS,
                 f"model text is forbidden in an LLM call record: {name!r}")
    trace: Dict[str, Any] = {
        "seq": seq, "stage": stage, "backendName": backend_name,
        "tUtc": None, "episodeId": None, "cycleId": None,
        "modelVersion": None, "promptHash": None, "latencyMs": None,
        "inputTokens": None, "outputTokens": None, "success": None,
        "schemaValid": None, "error": None,
    }
    trace.update(fields)
    if trace.get("error") is not None:
        trace["error"] = str(trace["error"])[:500]
    return trace


# --------------------------------------------------------------------------- #
# Metric index
# --------------------------------------------------------------------------- #


def metric_index_entry(*, status: str, source: str, scope_level: str, unit: str,
                       reason: Optional[str] = None,
                       gap_id: Optional[str] = None,
                       scope_ids: Sequence[str] = (),
                       sampling_interval_ms: Optional[int] = None,
                       sample_count: int = 0,
                       first_sample_at: Optional[str] = None,
                       last_sample_at: Optional[str] = None,
                       quality_counts: Optional[Mapping[str, int]] = None,
                       derived: bool = False) -> Dict[str, Any]:
    """Build one entry of ``normalized/metric-index.json``.

    The two rules this enforces are the mechanical form of §4's prohibition on
    synthesizing or substituting a KPI:

    1. a non-``OK`` status **must** state a reason - a metric that is simply
       missing from the UI, or present with an unexplained badge, teaches the
       operator nothing;
    2. an ``UNSUPPORTED`` / ``NOT_APPLICABLE`` metric must have
       ``sampleCount == 0``.  Samples under a metric this deployment has no
       source for would mean a different measurement had been substituted;
    3. an ``UNAVAILABLE`` metric may have rows - a source that answered with
       nothing still produced records - but none of them may have carried a
       usable value.  An ``OK``-quality sample under ``UNAVAILABLE`` would mean
       the status and the data disagree, and the status is what an operator
       reads.
    """
    _require(status in METRIC_STATUSES, f"unknown metric status {status!r}")
    _require(scope_level in SCOPE_LEVELS, f"unknown scope level {scope_level!r}")
    _require(status == "OK" or bool(reason),
             f"status {status} requires a reason")
    _require(sample_count >= 0, "sampleCount cannot be negative")
    counts = dict(quality_counts or {})
    _require(not (status in NO_SAMPLE_STATUSES and sample_count),
             f"status {status} cannot carry {sample_count} samples - "
             "an unsupported metric with data means something was substituted")
    _require(not (status == "UNAVAILABLE" and counts.get("OK")),
             "status UNAVAILABLE cannot carry OK-quality samples - "
             "the status and the data would disagree")
    return {
        "status": status,
        "reason": reason,
        "gapId": gap_id,
        "source": source,
        "scopeLevel": scope_level,
        "scopeIds": list(scope_ids),
        "unit": unit,
        "samplingIntervalMs": sampling_interval_ms,
        "sampleCount": int(sample_count),
        "firstSampleAt": first_sample_at,
        "lastSampleAt": last_sample_at,
        "qualityCounts": counts,
        "derived": bool(derived),
    }


def unsupported(source: str, scope_level: str, unit: str, reason: str,
                gap_id: Optional[str] = None) -> Dict[str, Any]:
    """No source for this metric exists in this deployment."""
    return metric_index_entry(status="UNSUPPORTED", source=source,
                              scope_level=scope_level, unit=unit,
                              reason=reason, gap_id=gap_id)


def unavailable(source: str, scope_level: str, unit: str, reason: str,
                gap_id: Optional[str] = None) -> Dict[str, Any]:
    """A source exists and was asked, but delivered nothing."""
    return metric_index_entry(status="UNAVAILABLE", source=source,
                              scope_level=scope_level, unit=unit,
                              reason=reason, gap_id=gap_id)


__all__ = [
    "ANNOTATABLE_KINDS", "EQ12_STATES", "FORBIDDEN_LLM_FIELDS", "LANES",
    "LLM_STAGES", "METRIC_STATUSES", "NO_SAMPLE_STATUSES", "ORIGINS",
    "QUALITIES", "RecordError", "SCOPE_LEVELS", "SEVERITIES",
    "SOURCE_BOUNDARIES", "TERMINAL_OUTCOMES", "TERMINAL_OUTCOME_TO_EQ12",
    "cycle_trace", "episode_trace", "llm_call_trace", "metric_index_entry",
    "telemetry_sample", "timeline_event", "unavailable", "unsupported",
]
