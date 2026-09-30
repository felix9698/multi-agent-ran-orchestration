"""Phase B Operator Console - view-model types.

FROZEN SEAM.  Design authority: ``docs/phase-b-gui/DESIGN.md`` section 3 and
``docs/phase-b-gui/file-ownership.1.0.0.json``.

These frozen dataclasses are the *only* shapes that cross from the data layer to
the render layer.  A workspace reads these and nothing else - it never reaches
into a source, a store or a coordinator result.  That single rule is what makes
the console's logic testable without a display, and what would make a renderer
swap a contained change rather than a rewrite.

This module must never import ``tkinter``.  It holds no behaviour beyond
validation and small derived properties.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Topology and health
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ComponentStatusView:
    """One logical O-RAN element in the topology view.

    Elements are enumerated from the RAN capability manifest, never hard-coded -
    phaseB_task.md section 6 forbids forcing a fixed component count or name onto
    a deployment.  ``status`` is a GUI status id from ``gui.operator.status``.

    ``version`` / ``release`` / ``profile`` carry what the manifest declares for
    this element.  When it declares nothing, the three stay ``None`` and the
    ``version_*`` trio states why in the status vocabulary's own words - a
    status id (``UNSUPPORTED`` or ``UNKNOWN``), the reason, and the
    integration-field-gaps.md gap id.  They are separate from ``status`` and
    ``gap_id`` on purpose: an element can be perfectly healthy and still have no
    declared provenance, and collapsing the two would either hide the missing
    provenance or slander a working element.
    """

    element_id: str
    label: str
    kind: str                      # GUI | RAPP | NONRT_RIC | R1 | A1_BOUNDARY |
                                   # NEARRT_RIC | XAPP | O1_PROVIDER | DME |
                                   # O_CU | O_DU | O_RU | GNB | CORE | UE | USRP
    status: str
    status_reason: Optional[str] = None
    readiness: Optional[str] = None
    source: Optional[str] = None           # which boundary reported this
    observed_at: Optional[str] = None      # UTC ISO-8601
    age_ms: Optional[float] = None
    freshness: Optional[str] = None
    version: Optional[str] = None
    release: Optional[str] = None
    profile: Optional[str] = None
    version_status: Optional[str] = None   # GUI status id for the version cell
    version_reason: Optional[str] = None
    version_gap_id: Optional[str] = None
    gap_id: Optional[str] = None
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReadinessSegmentView:
    """One segment of the intent -> policy -> evidence readiness strip.

    phaseB_task.md section 2 asks for more than a green light: the operator must
    see *which* segment of the chain is ready and which is blocked.
    """

    segment_id: str
    label: str
    status: str
    confirmed: bool = False
    at: Optional[str] = None
    owner: Optional[str] = None
    unmet_reason: Optional[str] = None
    blocked_by: Optional[str] = None


# --------------------------------------------------------------------------- #
# Intent and decision
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class IntentRowView:
    """One row of the intent table.

    The three status axes are three separate fields.  Merging them into one
    indicator is a specification violation: an intent can be Admitted while its
    policy is not yet enforced and its effect is not yet evidenced, and an
    operator must be able to see that.
    """

    intent_id: str
    revision: Optional[str]
    text: str
    target_scope: Optional[str] = None
    priority: Optional[str] = None
    validity: Optional[str] = None

    # axis 1 - coordinator
    intent_state: Optional[str] = None          # S0..S6 / S_TECHNICAL_FAILSAFE
    terminal_outcome: Optional[str] = None       # four-way internal outcome
    eq12_state: Optional[str] = None             # Admitted/NotAdmitted/TechnicalFailsafe

    # axis 2 - A1 policy status via R1
    policy_id: Optional[str] = None
    policy_type: Optional[str] = None
    policy_version: Optional[str] = None
    policy_status: Optional[str] = None          # GUI status id
    policy_status_detail: Optional[str] = None

    # axis 3 - DME/O1 evidence
    evidence_status: Optional[str] = None        # GUI status id
    evidence_observed_at: Optional[str] = None
    evidence_freshness: Optional[str] = None
    evidence_quality: Optional[str] = None

    last_decision_at: Optional[str] = None
    last_update_at: Optional[str] = None
    lifecycle: str = "ACTIVE"    # ACTIVE|PENDING|CONFLICT|NEGOTIATING|
                                 # SATISFIED|VIOLATED|WITHDRAWN


@dataclass(frozen=True)
class StageView:
    """One stage of the S0-S6 path or of the three-stage LLM pipeline."""

    stage_id: str
    label: str
    state: str                    # PENDING|RUNNING|DONE|FAILED|SKIPPED|WAITING
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    duration_ms: Optional[float] = None
    detail: Optional[str] = None


@dataclass(frozen=True)
class AlternativeView:
    """One negotiation candidate."""

    alternative_id: str
    description: str
    confidence: Optional[float] = None
    target_value: Optional[float] = None
    accepted: bool = False


@dataclass(frozen=True)
class DecisionView:
    """The structured agentic judgement for one episode.

    Two subtleties are represented explicitly because collapsing either would
    misdescribe the system:

    * routing is decided on ``calibrated_probability`` against ``theta_star``,
      never on ``raw_confidence`` - both are carried so the UI can show it;
    * an accepted alternative is an *agreement*, not a resolution.  ``agreement``
      and ``success`` are separate fields for that reason.
    """

    episode_id: str
    intent_text: str
    normalized_intent: Optional[Mapping[str, Any]] = None
    fsm_stages: Tuple[StageView, ...] = ()
    llm_stages: Tuple[StageView, ...] = ()

    goals: Tuple[str, ...] = ()
    constraints: Tuple[str, ...] = ()

    has_conflict: Optional[bool] = None          # None = GAP-03, never inferred
    conflict_intent_ids: Tuple[str, ...] = ()

    feasible: Optional[bool] = None
    raw_confidence: Optional[float] = None
    calibrated_probability: Optional[float] = None
    theta_star: Optional[float] = None
    threshold_applied_to: Optional[str] = None
    calibration_reason: Optional[str] = None
    routed_to: Optional[str] = None

    alternatives: Tuple[AlternativeView, ...] = ()
    negotiation_rounds: Optional[int] = None
    agreement: Optional[bool] = None
    rolled_back: Optional[bool] = None

    terminal_outcome: Optional[str] = None
    terminal_reason: Optional[str] = None
    eq12_state: Optional[str] = None
    success: Optional[bool] = None

    #: Model-authored short rationale.  Rendered through the untrusted-text path:
    #: hard length cap, "model text" marker, never a chain-of-thought panel.
    #: decision/llm_backend.py can place a thinking model's reasoning_content
    #: here via the schema-rejection paths, so it is never trusted.
    reasoning_summary: Optional[str] = None
    reasoning_truncated: bool = False

    latency_ms: Optional[float] = None
    stage_latencies: Mapping[str, Optional[float]] = field(default_factory=dict)


@dataclass(frozen=True)
class CorrelationTraceView:
    """One correlation id's trace across the eight items an operator must be
    able to follow from a submitted natural-language Intent to its terminal
    outcome (final integration section 4.7).

    Every item is independent and carries its own GUI status id and, when the
    underlying data was not observed, a reason - matching the console-wide
    rule that an absent value renders ``UNKNOWN``/``UNSUPPORTED`` with a
    reason and is never synthesised or borrowed from a different scope.  An
    operator may see the LLM verdict while the E2 write count is still
    Unknown; collapsing the eight into one status would hide exactly that.
    """

    correlation_id: Optional[str] = None
    correlation_id_status: str = "UNKNOWN"
    correlation_id_reason: Optional[str] = None

    # 1. original Intent + revision
    intent_id: Optional[str] = None
    intent_revision: Optional[str] = None
    intent_text: Optional[str] = None
    intent_status: str = "UNKNOWN"
    intent_reason: Optional[str] = None

    # 2. three-stage LLM judgement + final verdict
    llm_stages: Tuple["StageView", ...] = ()
    verdict_status: str = "UNKNOWN"
    verdict_detail: Optional[str] = None
    verdict_reason: Optional[str] = None

    # 3. generated rApp/R1 policy identity
    policy_id: Optional[str] = None
    policy_type_id: Optional[str] = None
    policy_revision: Optional[str] = None
    policy_identity_status: str = "UNKNOWN"
    policy_identity_reason: Optional[str] = None

    # 4. A1-P policy lifecycle/status
    policy_state: Optional[str] = None
    enforce_status: Optional[str] = None
    episode_state: Optional[str] = None
    policy_lifecycle_status: str = "UNKNOWN"
    policy_lifecycle_reason: Optional[str] = None

    # 5. selected UE + target cell
    ue_id: Optional[str] = None
    target_cells: Tuple[str, ...] = ()
    target_source: Optional[str] = None    # DISPATCHED | DECLARED_ONLY
    target_status: str = "UNKNOWN"
    target_reason: Optional[str] = None

    # 6. E2 control attempt + write count
    e2_control_attempted: Optional[bool] = None
    e2_control_result: Optional[str] = None
    e2_write_count: Optional[int] = None
    e2_status: str = "UNKNOWN"
    e2_reason: Optional[str] = None

    # 7. KPM effect readback + O1 assurance
    readback_result: Optional[str] = None
    assurance_decision: Optional[str] = None
    evidence_quality: Optional[str] = None
    assurance_status: str = "UNKNOWN"
    assurance_reason: Optional[str] = None

    # 8. Coordinator FSM terminal state + commit/rollback
    eq12_state: Optional[str] = None
    terminal_outcome: Optional[str] = None
    rolled_back: Optional[bool] = None
    fsm_status: str = "UNKNOWN"
    fsm_reason: Optional[str] = None


@dataclass(frozen=True)
class ObjectiveAdvertisementView:
    """One objective the bound deployment declares it does or does not
    execute.  ``executable`` false with ``a1_policy_type`` ``None`` is the
    shape an experimental extension takes - advertised for capability
    visibility, never combined with the frozen A1 UE Cell Steering policy.
    """

    objective: str
    executable: bool
    a1_policy_type: Optional[str] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class DeploymentProvenanceView:
    """Which composed release the bound deployment composes against, and which
    objectives it advertises as actually executable through it.

    Every field is independently honest: a deployment that is bound but not
    composed against a released component still reports ``composition_basis``
    (as whatever the integration declares, e.g. a development-only label)
    rather than leaving the field blank, and ``objectives_status`` is
    ``UNKNOWN`` with a reason whenever the advertising surface itself could
    not be read - never defaulted to an empty, silently-successful list.
    """

    composed_release_binding: Optional[str] = None
    composition_basis: Optional[str] = None
    objectives: Tuple[ObjectiveAdvertisementView, ...] = ()
    objectives_status: str = "UNKNOWN"
    objectives_reason: Optional[str] = None


@dataclass(frozen=True)
class LlmBackendView:
    """One selectable LLM backend.

    ``availability`` is a GUI status id.  ``is_available()`` upstream reflects
    only that a client object was constructed, so a fresh backend is UNKNOWN with
    reason CONSTRUCTION_ONLY and is promoted to OK only after an observed
    successful call (see integration-field-gaps.md GAP-11).
    """

    name: str
    model_version: Optional[str] = None
    availability: str = "UNKNOWN"
    availability_reason: Optional[str] = None
    last_success_at: Optional[str] = None
    last_latency_ms: Optional[float] = None
    is_active: bool = False
    #: Reference NAMES only, e.g. "env:ANTHROPIC_API_KEY".  Never a value.
    credential_refs: Tuple[str, ...] = ()
    credential_configured: Optional[bool] = None


@dataclass(frozen=True)
class CalibrationView:
    """Calibration and budget-guard state.

    ``total_*`` counters are global cross-partition aggregates - the engine says
    so via ``counters_scope`` - and are labelled as such.  Per-partition figures
    come from ``partition_counters``.
    """

    mode: Optional[str] = None
    theta_star: Optional[float] = None
    theta_star_raw: Optional[float] = None
    theta_star_clamped: Optional[bool] = None
    assumption_ok: Optional[bool] = None
    n_max: Optional[int] = None
    c_worst: Optional[float] = None
    c_nego: Optional[float] = None
    r_success: Optional[float] = None
    c_episode: Optional[float] = None
    counters_scope: Optional[str] = None
    total_episodes: Optional[int] = None
    total_trials: Optional[int] = None
    total_successes: Optional[int] = None
    total_rollbacks: Optional[int] = None
    total_negotiations: Optional[int] = None
    ece: Optional[float] = None
    brier: Optional[float] = None
    sample_count: Optional[int] = None
    sufficient_samples: Optional[bool] = None
    partition_counters: Mapping[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Telemetry and timeline
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MetricSampleView:
    """One telemetry point ready for a chart.

    ``value`` of ``None`` means honest-unknown: the chart draws a gap.  It is
    never plotted as zero and never interpolated across.
    """

    metric: str
    value: Optional[float]
    unit: str
    scope_level: str
    scope_id: str
    t_utc: Optional[str] = None
    t_rel_s: Optional[float] = None
    quality: str = "OK"
    status: str = "OK"
    source_class: str = "O1_DME"
    aggregation: Optional[str] = None
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    sampling_interval_ms: Optional[int] = None
    age_ms: Optional[float] = None
    freshness: Optional[str] = None
    derived: bool = False
    derivation: Optional[str] = None
    artifact_ref: Optional[str] = None


@dataclass(frozen=True)
class MetricAvailabilityView:
    """One row of the metric-availability panel.

    Unsupported metrics stay visible with their reason.  A metric that simply
    vanishes from the UI teaches the operator nothing.
    """

    metric: str
    display: str
    status: str
    reason: Optional[str] = None
    gap_id: Optional[str] = None
    source_class: Optional[str] = None
    scope_level: Optional[str] = None
    unit: Optional[str] = None
    sampling_interval_ms: Optional[int] = None
    sample_count: int = 0
    last_sample_at: Optional[str] = None
    derived: bool = False
    # -- added at integration, additively --------------------------------- #
    # The design step froze this view without a value field, and the Demo View
    # then had nothing to put on a headline KPI card - it showed a sample count,
    # which is not a KPI.  These three are appended, so every existing
    # construction and every field above is unchanged.
    #
    # The metric-registry discipline still holds and is why they come as a
    # group: a value is never carried without the availability and quality that
    # qualify it.  ``last_value`` is meaningful only when ``status`` is OK, and a
    # renderer that shows the number must show ``last_value_quality`` with it.
    last_value: Optional[float] = None
    last_value_quality: Optional[str] = None
    last_value_scope: Optional[str] = None


@dataclass(frozen=True)
class TimelineEventView:
    """One event on the operator timeline.

    ``origin`` of ``DERIVED`` marks an event the GUI inferred rather than
    observed - for example an FSM transition from a capture, which carries
    ordering but no timestamp.  Derived events render on a distinct track so they
    are never read as measured times.
    """

    seq: int
    lane: str
    kind: str
    severity: str = "INFO"
    origin: str = "OBSERVED"
    derivation: Optional[str] = None
    t_utc: Optional[str] = None
    t_rel_s: Optional[float] = None
    title: str = ""
    component: Optional[str] = None
    run_id: Optional[str] = None
    intent_id: Optional[str] = None
    policy_id: Optional[str] = None
    episode_id: Optional[str] = None
    evidence_id: Optional[str] = None
    correlation_id: Optional[str] = None
    annotatable: bool = False
    detail: Mapping[str, Any] = field(default_factory=dict)
    source_ref: Optional[str] = None


# --------------------------------------------------------------------------- #
# Session, profile and results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PreflightCheckView:
    """One preflight result.  Every check is read-only by construction."""

    check_id: str
    label: str
    status: str                 # OK | ERROR | UNSUPPORTED | UNKNOWN
    reason: Optional[str] = None
    boundary: Optional[str] = None
    detail: Optional[str] = None


@dataclass(frozen=True)
class ConfirmationSpec:
    """A destructive or experiment-affecting action awaiting acknowledgement.

    phaseB_task.md section 1 requires the current target and the effect to be
    shown before the operator confirms.  ``TYPED_CONFIRM`` is used where the
    action cannot be undone.
    """

    action_id: str
    title: str
    targets: Tuple[str, ...]
    effects: Tuple[str, ...]
    acknowledgement: str = "SINGLE_CONFIRM"   # or TYPED_CONFIRM
    typed_phrase: Optional[str] = None
    irreversible: bool = False


@dataclass(frozen=True)
class RunSummaryView:
    """Headline results for one run.

    ``disposition`` is carried verbatim from the manifest.  Only ``COMPLETED``
    is a success; ABORTED, FAILED and INTERRUPTED keep their data and never
    render as success.
    """

    run_id: str
    mode: str
    disposition: str
    profile_id: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    elapsed_s: Optional[float] = None
    episodes: int = 0
    admitted: int = 0
    not_admitted: int = 0
    technical_failsafe: int = 0
    negotiations: int = 0
    rollbacks: int = 0
    policies: int = 0
    evidence_records: int = 0
    mean_latency_ms: Optional[float] = None
    data_issues: Tuple[str, ...] = ()
    warnings: int = 0
    errors: int = 0


@dataclass(frozen=True)
class SessionState:
    """The complete snapshot a workspace renders from.

    This is the single object handed to ``Workspace.on_state``.  A workspace that
    needs something not on here asks for the field to be added rather than
    reaching around the boundary.
    """

    run_id: Optional[str] = None
    #: ``DISCONNECTED`` until a session starts: the console has contacted
    #: nothing, so it claims nothing.  A session's mode comes from its source
    #: and is one of the store's modes; there is no control that sets it.
    mode: str = "DISCONNECTED"
    #: ``IDLE`` matches the controller's own starting disposition.  Defaulting
    #: to RUNNING made a console that had started nothing read as a recording
    #: run in the header until the first state was published.
    disposition: str = "IDLE"
    profile_id: Optional[str] = None
    profile_valid: Optional[bool] = None
    started_at: Optional[str] = None
    elapsed_s: Optional[float] = None
    recording: bool = False
    recorded_bytes: int = 0

    health: str = "UNKNOWN"
    health_counts: Mapping[str, int] = field(default_factory=dict)
    components: Tuple[ComponentStatusView, ...] = ()
    readiness: Tuple[ReadinessSegmentView, ...] = ()

    intents: Tuple[IntentRowView, ...] = ()
    decision: Optional[DecisionView] = None
    correlation_trace: Optional[CorrelationTraceView] = None
    deployment_provenance: Optional[DeploymentProvenanceView] = None
    calibration: Optional[CalibrationView] = None
    llm_backends: Tuple[LlmBackendView, ...] = ()
    active_backend: Optional[str] = None

    metrics: Tuple[MetricAvailabilityView, ...] = ()
    timeline: Tuple[TimelineEventView, ...] = ()
    summary: Optional[RunSummaryView] = None

    last_warning: Optional[TimelineEventView] = None
    pending_confirmation: Optional[ConfirmationSpec] = None
    dropped_updates: int = 0

    @property
    def is_live(self) -> bool:
        """True only for a genuinely live run.

        Read this rather than comparing strings ad hoc, so that no workspace can
        accidentally treat a Replay session as Live.
        """
        return self.mode == "LIVE"


@dataclass(frozen=True)
class SeriesSpec:
    """Identity and styling for one chart series."""

    series_id: str
    label: str
    color: str
    linestyle: str = "-"
    marker: str = ""
    unit: str = ""
    source_class: str = ""
    scope: str = ""
    derived: bool = False
    status: str = "OK"
    status_reason: Optional[str] = None


@dataclass(frozen=True)
class ColumnSpec:
    """One column of a dense table."""

    key: str
    heading: str
    width: int = 120
    anchor: str = "w"
    kind: str = "text"           # text | status | number | time | age
    unit: Optional[str] = None
    decimals: int = 2


@dataclass(frozen=True)
class FigureExportResult:
    """What an export produced, for the store and for the operator's feedback."""

    figure_id: str
    paths: Tuple[str, ...]
    source_csv: str
    metadata_path: str
    mode: str
    derived_visualization: bool = False


__all__ = [
    "AlternativeView", "CalibrationView", "ColumnSpec", "ComponentStatusView",
    "ConfirmationSpec", "CorrelationTraceView", "DecisionView",
    "DeploymentProvenanceView", "FigureExportResult", "IntentRowView",
    "LlmBackendView", "MetricAvailabilityView", "MetricSampleView",
    "ObjectiveAdvertisementView", "PreflightCheckView", "ReadinessSegmentView",
    "RunSummaryView", "SeriesSpec", "SessionState", "StageView",
    "TimelineEventView",
]
