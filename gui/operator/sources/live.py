"""Live intent source and pure decision projections.

The write path is intentionally narrow: an intent episode enters through the
one episode entry the composition handed in.  This module imports no episode
runtime of its own - that is what made the pre-Kernel decision runtime
reachable from inside the console package, and the cutover replaced it with an
injected ``runner``.  Everything else here is a pure projection over returned
records and can be tested without a display or a deployment.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import threading
from typing import Any, Callable, Mapping, Optional, Sequence

from gui.operator.status import (
    PRE_MEASUREMENT,
    map_contract_value,
    terminal_state_label,
)
from gui.operator.viewmodel.types import (
    AlternativeView,
    CalibrationView,
    ConfirmationSpec,
    CorrelationTraceView,
    DecisionView,
    DeploymentProvenanceView,
    IntentRowView,
    ObjectiveAdvertisementView,
    StageView,
)
from gui.operator.widgets.modeltext import sanitize_model_text


POLICY_TYPE = "AIC_UECellSteering_1.0.0"
POLICY_VERSION = "1.0.0"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)


def _dig(value: Any, *paths: str) -> Any:
    """Find the first dotted mapping path, accepting snake/camel wire forms."""
    for path in paths:
        current = value
        found = True
        for part in path.split("."):
            if isinstance(current, Mapping) and part in current:
                current = current[part]
            else:
                found = False
                break
        if found:
            return current
    return None


def _tuple_text(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        return tuple(f"{key}={item}" for key, item in value.items())
    if isinstance(value, Sequence):
        return tuple(str(item) for item in value)
    return (str(value),)


@dataclass(frozen=True)
class IntentSubmission:
    integration_path: str
    request: Mapping[str, Any]
    target_scope: str = PRE_MEASUREMENT
    objective: str = PRE_MEASUREMENT
    constraints: tuple[str, ...] = ()
    priority: str = "MEDIUM"
    validity: str = PRE_MEASUREMENT
    policy_type: str = POLICY_TYPE
    policy_revision: str = "1"

    @property
    def intent_text(self) -> str:
        text = self.request.get("intentText")
        return str(text).strip() if text is not None else ""


@dataclass(frozen=True)
class IntentSubmissionResult:
    authoritative: Mapping[str, Any]
    decision: DecisionView
    intent: IntentRowView
    submitted_at: str


def normalized_intent_preview(parsed: Any, *,
                              validate: Optional[Callable[[Mapping[str, Any]],
                                                          Mapping[str, Any]]]
                              = None) -> Optional[Mapping[str, Any]]:
    """Validate an episode-produced parse result for preview.

    The GUI never performs a second natural-language parse.  Callers pass the
    mapping the episode runtime's parser produced, together with *that
    runtime's own* strict validator; it either accepts the mapping or the
    preview stays unknown.

    ``validate`` is injected rather than imported.  Importing it here is what
    put ``coordinator.schema`` - and the whole pre-Kernel decision runtime
    behind it - inside the console package; with no validator supplied there is
    nothing to check against, and an unchecked parse is reported as unknown
    rather than passed through as if it had been validated.
    """
    if not isinstance(parsed, Mapping) or validate is None:
        return None
    try:
        return deepcopy(validate(dict(parsed)))
    except Exception:
        return None


def build_submit_confirmation(submission: IntentSubmission, *,
                              run_id: Optional[str]) -> ConfirmationSpec:
    return ConfirmationSpec(
        action_id="C-INTENT-SUBMIT", title="Submit intent",
        targets=(f"Scope: {submission.target_scope}",
                 f"Objective: {submission.objective}",
                 f"Run: {run_id or PRE_MEASUREMENT}"),
        effects=(
            "Constraints: " + (", ".join(submission.constraints) or PRE_MEASUREMENT),
            f"Policy: {submission.policy_type} revision {submission.policy_revision}",
            "A real policy lifecycle may be created by the Non-RT RIC via R1.",
        ), acknowledgement="SINGLE_CONFIRM")


def build_withdraw_confirmation(row: IntentRowView) -> ConfirmationSpec:
    phrase = f"WITHDRAW {row.intent_id}"
    return ConfirmationSpec(
        action_id="C-INTENT-WITHDRAW", title="Withdraw intent",
        targets=(f"Intent: {row.intent_id} revision {row.revision or PRE_MEASUREMENT}",
                 f"Policy: {row.policy_id or PRE_MEASUREMENT}"),
        effects=(f"Current policy: {row.policy_status or PRE_MEASUREMENT}",
                 f"Current evidence: {row.evidence_status or PRE_MEASUREMENT}",
                 "The bound policy is removed through its lifecycle; the local row is not merely cleared."),
        acknowledgement="TYPED_CONFIRM", typed_phrase=phrase,
        irreversible=True)


def build_llm_switch_confirmation(current: Optional[str], target: str) -> ConfirmationSpec:
    return ConfirmationSpec(
        action_id="C-LLM-SWITCH", title="Change LLM backend / model",
        targets=(f"Current: {current or PRE_MEASUREMENT}", f"Target: {target}"),
        effects=("The switch is audited.",
                 "It takes effect from the next episode/proposal boundary."),
        acknowledgement="SINGLE_CONFIRM")


def _fsm_stages(result: Mapping[str, Any]) -> tuple[StageView, ...]:
    explicit = result.get("fsm_stages")
    if isinstance(explicit, Sequence) and not isinstance(explicit, (str, bytes)):
        stages = []
        for item in explicit:
            data = _mapping(item)
            sid = str(_first(data.get("stage_id"), data.get("stageId"), data.get("id"), "UNKNOWN"))
            stages.append(StageView(
                stage_id=sid, label=str(data.get("label") or sid),
                state=str(data.get("state") or "PENDING"),
                started_at=_first(data.get("started_at"), data.get("startedAt")),
                ended_at=_first(data.get("ended_at"), data.get("endedAt")),
                duration_ms=_first(data.get("duration_ms"), data.get("durationMs")),
                detail=data.get("detail")))
        return tuple(stages)

    visited: set[str] = set()
    timing: dict[str, dict[str, Any]] = {}
    # ``fsmPath`` is the stored EpisodeTrace spelling.  A projection that only
    # knew the coordinator's live spellings could not read a run back from the
    # store, which is the whole of Replay.
    transitions = _first(result.get("fsm_history"), result.get("fsmHistory"),
                         result.get("fsmPath"), result.get("transitions"), ())
    if isinstance(transitions, Sequence) and not isinstance(transitions, (str, bytes)):
        for item in transitions:
            if isinstance(item, Mapping):
                source = str(item.get("from")) if item.get("from") else None
                target = str(item.get("to")) if item.get("to") else None
                visited.update(value for value in (source, target) if value)
                observed = _first(item.get("observed_at"), item.get("observedAt"), item.get("at"))
                mono = _first(item.get("monotonic_s"), item.get("monotonicS"))
                if source:
                    timing.setdefault(source, {})["ended_at"] = observed
                    timing[source]["ended_mono"] = mono
                if target:
                    timing.setdefault(target, {}).setdefault("started_at", observed)
                    timing[target].setdefault("started_mono", mono)
            elif item:
                visited.add(str(item))
    current = _first(result.get("fsm_state"), result.get("fsmState"))
    if current:
        visited.add(str(current))
    stored_outcome = _first(result.get("terminal_outcome"),
                            result.get("terminalOutcome"))
    if stored_outcome and stored_outcome != "technical_failsafe":
        visited.add("S6")
    cycle = _mapping((result.get("cycles") or [{}])[-1] if result.get("cycles") else {})
    latencies = _mapping(_first(result.get("stage_latencies"), cycle.get("stage_latencies"), {}))
    labels = {
        "S0": "Idle", "S1": "Conflict screening", "S2": "Feasibility",
        "S3": "Trial / policy", "S4": "Validation", "S5": "Negotiation",
        "S6": "Resolution", "S_TECHNICAL_FAILSAFE": "Technical failsafe",
    }
    order = ["S0", "S1", "S2", "S3", "S4", "S5", "S6"]
    if "S_TECHNICAL_FAILSAFE" in visited or stored_outcome == "technical_failsafe":
        order.append("S_TECHNICAL_FAILSAFE")
    stages = []
    for sid in order:
        duration = _first(latencies.get(sid), latencies.get(sid.lower()),
                          cycle.get(f"{sid.lower()}_ms"))
        state = "DONE" if sid in visited else "PENDING"
        if sid == current and sid not in {"S0", "S6", "S_TECHNICAL_FAILSAFE"}:
            state = "RUNNING"
        if sid == "S_TECHNICAL_FAILSAFE" and sid in order:
            state = "FAILED"
        stage_time = timing.get(sid, {})
        if duration is None:
            start_mono = stage_time.get("started_mono")
            end_mono = stage_time.get("ended_mono")
            if isinstance(start_mono, (int, float)) and isinstance(end_mono, (int, float)):
                duration = max(0.0, (float(end_mono) - float(start_mono)) * 1000.0)
        stages.append(StageView(
            sid, labels[sid], state,
            started_at=stage_time.get("started_at"),
            ended_at=stage_time.get("ended_at"), duration_ms=duration))
    return tuple(stages)


def _llm_stages(result: Mapping[str, Any], cycle: Mapping[str, Any]) -> tuple[StageView, ...]:
    terminal = _first(result.get("terminal_outcome"),
                      result.get("terminalOutcome")) is not None
    feasibility_observed = any(value is not None for value in (
        result.get("feasible"), cycle.get("feasible"),
        result.get("raw_confidence"), cycle.get("raw_confidence"),
        result.get("calibrated_probability"), cycle.get("calibrated_probability"),
        cycle.get("confidence"),
    ))
    alternatives_observed = bool(_first(
        result.get("alternatives"), cycle.get("alternatives"),
        result.get("negotiation_rounds"), cycle.get("negotiation_rounds")))
    values = (
        ("parse", "Parse / normalize",
         _first(result.get("parse_ms"), cycle.get("parse_ms")), terminal),
        ("feasibility", "Feasibility",
         _first(result.get("inference_ms"), cycle.get("inference_ms"),
                cycle.get("prompt_ms")), feasibility_observed),
        ("alternatives", "Alternatives",
         _first(result.get("negotiation_ms"), cycle.get("negotiation_ms")),
         alternatives_observed),
    )
    return tuple(StageView(
        stage_id=sid, label=label,
        state=("DONE" if duration is not None or observed else
               "SKIPPED" if terminal else "WAITING"),
        duration_ms=duration)
        for sid, label, duration, observed in values)


def _alternatives(value: Any) -> tuple[AlternativeView, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    output = []
    for index, item in enumerate(value):
        data = _mapping(item.to_dict() if hasattr(item, "to_dict") else item)
        expected = _mapping(data.get("expected_results"))
        output.append(AlternativeView(
            alternative_id=str(data.get("id") or f"alternative-{index + 1}"),
            description=str(data.get("description") or PRE_MEASUREMENT),
            confidence=data.get("confidence"),
            target_value=_first(data.get("target_value"), expected.get("target_value")),
            accepted=bool(data.get("accepted", False))))
    return tuple(output)


def project_decision(result: Mapping[str, Any], *,
                     intent_text: Optional[str] = None,
                     normalized_intent: Optional[Mapping[str, Any]] = None,
                     validate_intent: Optional[Callable[[Mapping[str, Any]],
                                                        Mapping[str, Any]]]
                     = None) -> DecisionView:
    """Project an authoritative episode without mutating or reinterpreting it.

    ``validate_intent`` is the episode runtime's own strict parse validator,
    supplied by whoever composed the episode.  Without it the recorded parse is
    left unshown rather than displayed as though something had checked it.
    """
    data = _mapping(result)
    if normalized_intent is None:
        normalized_intent = normalized_intent_preview(
            _first(data.get("normalized_intent"), data.get("normalizedIntent"),
                   data.get("parsed_intent"), data.get("parsedIntent")),
            validate=validate_intent)
    cycles = data.get("cycles")
    cycle = _mapping(cycles[-1] if isinstance(cycles, Sequence) and cycles else {})
    calibration = _mapping(_first(data.get("calibration"), cycle.get("calibration_context"), {}))
    agreement_data = _mapping(data.get("agreement"))
    negotiation = _mapping(_first(data.get("nego_stats"), cycle.get("nego_stats"), {}))
    raw_reasoning = _first(
        cycle.get("reasoning"), _dig(cycle, "feasibility.reasoning"),
        data.get("reasoning"), data.get("schema_reject_reason"), data.get("error"))
    safe_reasoning = sanitize_model_text(raw_reasoning)
    outcome = _first(data.get("terminal_outcome"), data.get("terminalOutcome"))
    raw = _first(data.get("raw_confidence"), cycle.get("raw_confidence"),
                 calibration.get("raw_confidence"), cycle.get("confidence"))
    calibrated = _first(data.get("calibrated_probability"),
                        cycle.get("calibrated_probability"),
                        calibration.get("calibrated_probability"))
    theta = _first(data.get("threshold"), data.get("theta_star"),
                   cycle.get("threshold"), cycle.get("theta_star"),
                   calibration.get("threshold"))
    threshold_applied_to = _first(
        data.get("threshold_applied_to"), cycle.get("threshold_applied_to"),
        calibration.get("threshold_applied_to"))
    # A missing persisted S1 verdict stays Unknown (GAP-03); never infer it from
    # a path that necessarily crosses S1.
    conflict = _first(data.get("hasConflict"), data.get("has_conflict"))
    if not isinstance(conflict, bool):
        conflict = None
    alternatives = _first(data.get("alternatives"), cycle.get("alternatives"),
                          negotiation.get("alternatives"), ())
    goals = _first(_dig(normalized_intent or {}, "goals"),
                   _dig(normalized_intent or {}, "target"), data.get("goals"))
    constraints = _first(_dig(normalized_intent or {}, "constraints"),
                         data.get("constraints"))
    latency = _first(data.get("latency_ms"), data.get("latencyMs"),
                     data.get("duration_ms"), cycle.get("latency_ms"))
    return DecisionView(
        episode_id=str(_first(data.get("episode_id"), data.get("episodeId"), "unknown")),
        intent_text=str(_first(intent_text, data.get("intent_text"), data.get("intentText"), "")),
        normalized_intent=deepcopy(normalized_intent) if normalized_intent is not None else None,
        fsm_stages=_fsm_stages(data), llm_stages=_llm_stages(data, cycle),
        goals=_tuple_text(goals), constraints=_tuple_text(constraints),
        has_conflict=conflict,
        conflict_intent_ids=_tuple_text(_first(
            data.get("conflict_intent_ids"), data.get("conflictIntentIds"),
            _dig(data, "joint_evaluation.conflict_ids"),
            _dig(cycle, "joint_evaluation.conflict_ids"))),
        feasible=_first(data.get("feasible"), cycle.get("feasible")),
        raw_confidence=raw, calibrated_probability=calibrated,
        theta_star=theta, threshold_applied_to=threshold_applied_to,
        calibration_reason=_first(data.get("calibration_reason"), cycle.get("calibration_reason")),
        routed_to=_first(data.get("routed_to"), cycle.get("routed_to")),
        alternatives=_alternatives(alternatives),
        negotiation_rounds=_first(data.get("negotiation_rounds"),
                                  data.get("negotiationRounds"),
                                  negotiation.get("rounds"), cycle.get("negotiation_rounds")),
        agreement=_first(agreement_data.get("accepted"), data.get("agreement_accepted")),
        rolled_back=_first(data.get("rolled_back"), data.get("rolledBack"),
                           cycle.get("rolled_back")),
        terminal_outcome=outcome,
        terminal_reason=_first(data.get("terminal_reason"),
                               data.get("terminalReason")),
        eq12_state=terminal_state_label(outcome), success=data.get("success"),
        reasoning_summary=safe_reasoning.text or None,
        reasoning_truncated=safe_reasoning.truncated or safe_reasoning.suppressed,
        latency_ms=latency,
        stage_latencies=deepcopy(_mapping(_first(data.get("stage_latencies"), cycle.get("stage_latencies"), {}))))


def _policy_gui_status(data: Mapping[str, Any]) -> tuple[str, Optional[str]]:
    policy = _mapping(_first(data.get("policy_status"), data.get("policyStatus"),
                            _dig(data, "profile_trial.policy_status"), {}))
    enforce = _first(policy.get("enforceStatus"), data.get("enforceStatus"))
    state = _first(_dig(policy, "aicStatus.policyState"), policy.get("policyState"),
                   data.get("policyState"))
    episode = _first(_dig(policy, "aicStatus.episodeState"), policy.get("episodeState"),
                     data.get("episodeState"))
    observed = []
    for table, value in (("episodeState", episode), ("policyState", state),
                         ("enforceStatus", enforce)):
        if value is not None:
            resolved = map_contract_value(table, value)
            observed.append((resolved, str(value)))
    if observed:
        resolved, detail = max(observed, key=lambda item: item[0].severity)
        return resolved.status, detail
    return "UNKNOWN", "No R1 policy status observed"


def _evidence_gui_status(data: Mapping[str, Any]) -> tuple[str, Optional[str], Optional[str]]:
    decision = _first(data.get("assurance_decision"), data.get("assuranceDecision"),
                      _dig(data, "profile_trial.judgement"))
    quality = _first(data.get("evidence_quality"), data.get("evidenceQuality"),
                     _dig(data, "evidence.quality"))
    if decision is not None:
        resolved = map_contract_value("assuranceDecision", decision)
        return resolved.status, str(decision), quality
    if quality is not None:
        resolved = map_contract_value("evidenceQuality", quality)
        return resolved.status, str(quality), str(quality)
    return "UNKNOWN", "No DME/O1 evidence observed", None


def project_intent_row(result: Mapping[str, Any], *, intent_text: str = "",
                       submitted_at: Optional[str] = None,
                       target_scope: Optional[str] = None,
                       priority: Optional[str] = None,
                       validity: Optional[str] = None) -> IntentRowView:
    data = _mapping(result)
    cycles = data.get("cycles")
    cycle = _mapping(cycles[-1] if isinstance(cycles, Sequence) and cycles else {})
    trial = _mapping(_first(cycle.get("profile_trial"), data.get("profile_trial"), {}))
    policy = _mapping(trial.get("policy"))
    trace = _mapping(policy.get("trace"))
    policy_status, policy_detail = _policy_gui_status({**data, **cycle, **trial})
    evidence_status, evidence_detail, evidence_quality = _evidence_gui_status({**data, **cycle, **trial})
    outcome = _first(data.get("terminal_outcome"), data.get("terminalOutcome"))
    eq12 = terminal_state_label(outcome)
    lifecycle = "ACTIVE"
    if eq12 == "NotAdmitted":
        lifecycle = "PENDING"
    elif eq12 == "TechnicalFailsafe":
        lifecycle = "FAILSAFE"
    elif evidence_detail == "VIOLATED":
        lifecycle = "VIOLATED"
    elif evidence_detail == "SATISFIED":
        lifecycle = "SATISFIED"
    elif _first(data.get("negotiation_rounds"), _dig(data, "nego_stats.rounds"), 0):
        lifecycle = "NEGOTIATING"
    timestamp = submitted_at or _utc_now()
    return IntentRowView(
        intent_id=str(_first(trace.get("intentId"), data.get("intent_id"), data.get("intentId"), "unknown")),
        revision=_first(trace.get("intentRevision"), data.get("intent_revision"), data.get("intentRevision")),
        text=intent_text or str(_first(data.get("intent_text"), data.get("intentText"), "")),
        target_scope=target_scope or _first(data.get("target_scope"), data.get("scope")),
        priority=priority or data.get("priority"), validity=validity or data.get("validity"),
        intent_state=str(_first(
            data.get("fsm_state"), data.get("fsmState"),
            "S_TECHNICAL_FAILSAFE" if outcome == "technical_failsafe" else
            "S6" if outcome else PRE_MEASUREMENT)),
        terminal_outcome=outcome, eq12_state=eq12,
        policy_id=_first(trial.get("policyId"), data.get("policy_id"), data.get("policyId")),
        policy_type=_first(policy.get("policyTypeId"), data.get("policy_type"), POLICY_TYPE),
        policy_version=_first(data.get("policy_version"), data.get("policyVersion"), POLICY_VERSION),
        policy_status=policy_status, policy_status_detail=policy_detail,
        evidence_status=evidence_status,
        evidence_observed_at=_first(data.get("evidence_observed_at"), data.get("observedAt")),
        evidence_freshness=_first(data.get("evidence_freshness"), data.get("freshness"), "UNKNOWN"),
        evidence_quality=evidence_quality,
        last_decision_at=timestamp, last_update_at=timestamp, lifecycle=lifecycle)


def project_calibration(stats: Mapping[str, Any]) -> CalibrationView:
    data = _mapping(stats.get("calibration") if "calibration" in stats else stats)
    operating = _mapping(data.get("operating_context"))
    selected = {}
    for item in data.get("calibration_partitions") or ():
        part = _mapping(item)
        if (part.get("model_id") == operating.get("model_id")
                and part.get("regime_id") == operating.get("regime_id")):
            selected = part
            break
    return CalibrationView(
        mode=data.get("mode"), theta_star=data.get("theta_star"),
        theta_star_raw=data.get("theta_star_raw"),
        theta_star_clamped=(data.get("theta_star_raw") != data.get("theta_star")
                            if data.get("theta_star_raw") is not None else None),
        assumption_ok=data.get("assumption_ok"), n_max=data.get("n_max"),
        c_worst=data.get("c_worst_estimate"), c_nego=data.get("c_nego_estimate"),
        r_success=data.get("r_success_estimate"), c_episode=data.get("c_episode"),
        counters_scope=data.get("counters_scope"),
        total_episodes=data.get("total_episodes"), total_trials=data.get("total_trials"),
        total_successes=data.get("total_successes"), total_rollbacks=data.get("total_rollbacks"),
        total_negotiations=data.get("total_negotiations"),
        ece=selected.get("ece"), brier=selected.get("brier"),
        sample_count=_first(selected.get("sample_count"), selected.get("n")),
        sufficient_samples=selected.get("sufficient_samples"),
        partition_counters=deepcopy(_mapping(data.get("active_partition_counters"))))


def _ue_label(ue_id: Any) -> Optional[str]:
    """Render a contract ``UeId`` (``scope.ueId`` / ``context.ueId``) for display."""
    inner = _mapping(_mapping(ue_id).get("guAmfUeNgapId"))
    identity = inner.get("amfUeNgapId")
    return f"amfUeNgapId={identity}" if identity is not None else None


def _cell_label(cell: Any) -> Optional[str]:
    """Render a contract ``CellId`` as ``mcc-mnc/ncI``.  Mirrors the identical
    helper in ``oran/rapp/status_projection.py`` - not imported from there
    because that module is a frozen read-only boundary with its own owner and
    this is display formatting, not a projection of a transport read."""
    mapping = _mapping(cell)
    plmn = _mapping(mapping.get("plmnId"))
    ncid = _mapping(mapping.get("cId")).get("ncI")
    mcc, mnc = plmn.get("mcc"), plmn.get("mnc")
    if mcc and mnc and ncid is not None:
        return f"{mcc}-{mnc}/{ncid}"
    return str(ncid) if ncid is not None else None


def project_correlation_trace(*, authoritative: Mapping[str, Any],
                              decision: DecisionView,
                              intent_row: IntentRowView,
                              intent_id: str,
                              r1_outbound: Mapping[str, Any],
                              policy_status: Mapping[str, Any],
                              policy_context: Mapping[str, Any]) -> CorrelationTraceView:
    """Assemble one correlation id's eight-item trace, honestly.

    Every item is read from data the console already receives at the boundary
    it is scoped to - the dispatched policy object, the R1 outbound summary,
    the A1 status document, the coordinator's own decision - and nothing here
    calls a transport or invents a value.  An item whose source document was
    never observed renders ``UNKNOWN`` with a stated reason rather than being
    left silently absent or borrowed from a different scope.
    """
    data = _mapping(authoritative)
    cycles = data.get("cycles")
    cycle = _mapping(cycles[-1] if isinstance(cycles, Sequence) and cycles else {})
    trial = _mapping(_first(cycle.get("profile_trial"), data.get("profile_trial"), {}))
    policy = _mapping(trial.get("policy"))
    trace = _mapping(policy.get("trace"))
    aic = _mapping(policy_status.get("aicStatus"))
    control = _mapping(aic.get("control"))
    readback = _mapping(aic.get("readback"))
    rollback = _mapping(aic.get("rollback"))

    correlation_id = _first(trace.get("correlationId"), r1_outbound.get("correlationId"))
    if correlation_id:
        correlation_status, correlation_reason = "OK", None
    else:
        correlation_id = intent_id
        correlation_status = "UNKNOWN"
        correlation_reason = ("no policy was dispatched to R1; showing the "
                              "console-minted intent id, not a contract "
                              "correlationId")

    # 1. original Intent + revision
    if trace.get("intentId"):
        intent_status, intent_reason = "OK", None
    else:
        intent_status = "UNKNOWN"
        intent_reason = ("no policy trace was recorded; showing the "
                         "submission id, not the contract intentId")

    # 2. three-stage LLM judgement + final verdict
    if decision.eq12_state:
        verdict_status, verdict_reason = "OK", None
    else:
        verdict_status, verdict_reason = "UNKNOWN", "terminal outcome not yet observed"

    # 3. generated rApp/R1 policy identity
    policy_id = r1_outbound.get("policyId")
    contract_valid = r1_outbound.get("contractValid")
    if policy_id and contract_valid:
        policy_identity_status, policy_identity_reason = "OK", None
    elif policy_id:
        policy_identity_status = "ERROR"
        policy_identity_reason = r1_outbound.get("contractReason")
    else:
        policy_identity_status = "UNKNOWN"
        policy_identity_reason = (r1_outbound.get("contractReason")
                                  or "no policy object was dispatched")

    # 4. A1-P policy lifecycle/status
    policy_lifecycle_status, policy_lifecycle_reason = _policy_gui_status(
        {"policy_status": policy_status})

    # 5. selected UE + target cell - the dispatched object first, the
    # declared (pre-submission) scope only when nothing was dispatched.
    scope = _mapping(policy.get("scope"))
    ue_id_map = scope.get("ueId") if scope else None
    envelope = _mapping(_dig(policy, "steeringObjective.actionEnvelope"))
    allowed_cells = envelope.get("allowedCells") if envelope else None
    if ue_id_map or allowed_cells:
        target_source, target_status, target_reason = "DISPATCHED", "OK", None
    else:
        target_source = "DECLARED_ONLY"
        ue_id_map = policy_context.get("ueId")
        allowed_cells = policy_context.get("allowedCells")
        target_status = "UNKNOWN"
        target_reason = ("no policy was dispatched; showing the declared "
                         "scope, not a confirmed target")
    ue_label = _ue_label(ue_id_map)
    cell_labels = tuple(label for label in
                        (_cell_label(cell) for cell in (allowed_cells or ()))
                        if label)
    if not ue_label and not cell_labels:
        target_status, target_reason = "UNKNOWN", "no UE or cell scope was observed"

    # 6. E2 control attempt + write count.  The contract carries no
    # cumulative counter; the count is derived, per episode, from the two
    # writes the status schema can attest to: the original control write and
    # a rollback write that was actually sent.
    if control:
        e2_attempted = True
        e2_write_count = ((1 if control.get("writeMayHaveOccurred") is True else 0)
                          + (1 if rollback.get("state") in ("SENT", "VERIFIED", "FAILED") else 0))
        e2_status, e2_reason = "OK", None
    elif policy_status:
        e2_attempted, e2_write_count = False, 0
        e2_status, e2_reason = "OK", None
    else:
        e2_attempted, e2_write_count = None, None
        e2_status = "UNKNOWN"
        e2_reason = "no A1 policy status document was observed for this episode"

    # 7. KPM effect readback + O1 assurance
    readback_result = readback.get("result")
    assurance_status, assurance_detail, evidence_quality = _evidence_gui_status(
        {**data, **cycle, **trial})
    assurance_reason = (None if readback_result is not None or assurance_status != "UNKNOWN"
                        else "no readback or DME/O1 evidence was observed")

    # 8. Coordinator FSM terminal state + commit/rollback
    if decision.eq12_state:
        fsm_status, fsm_reason = "OK", None
    else:
        fsm_status, fsm_reason = "UNKNOWN", "coordinator has not reached a terminal state"

    return CorrelationTraceView(
        correlation_id=correlation_id, correlation_id_status=correlation_status,
        correlation_id_reason=correlation_reason,
        intent_id=_first(trace.get("intentId"), intent_row.intent_id, intent_id),
        intent_revision=_first(trace.get("intentRevision"), intent_row.revision),
        intent_text=intent_row.text or decision.intent_text or None,
        intent_status=intent_status, intent_reason=intent_reason,
        llm_stages=decision.llm_stages,
        verdict_status=verdict_status, verdict_detail=decision.eq12_state,
        verdict_reason=verdict_reason,
        policy_id=policy_id, policy_type_id=r1_outbound.get("policyTypeId"),
        policy_revision=_first(trace.get("policyRevision"), r1_outbound.get("policyRevision")),
        policy_identity_status=policy_identity_status,
        policy_identity_reason=policy_identity_reason,
        policy_state=aic.get("policyState"), enforce_status=policy_status.get("enforceStatus"),
        episode_state=aic.get("episodeState"),
        policy_lifecycle_status=policy_lifecycle_status,
        policy_lifecycle_reason=policy_lifecycle_reason,
        ue_id=ue_label, target_cells=cell_labels, target_source=target_source,
        target_status=target_status, target_reason=target_reason,
        e2_control_attempted=e2_attempted, e2_control_result=control.get("result"),
        e2_write_count=e2_write_count, e2_status=e2_status, e2_reason=e2_reason,
        readback_result=readback_result, assurance_decision=assurance_detail,
        evidence_quality=evidence_quality, assurance_status=assurance_status,
        assurance_reason=assurance_reason,
        eq12_state=decision.eq12_state, terminal_outcome=decision.terminal_outcome,
        rolled_back=decision.rolled_back, fsm_status=fsm_status, fsm_reason=fsm_reason)


def _objective_view(item: Any) -> Optional[ObjectiveAdvertisementView]:
    """One entry from a plain sequence, as a last-resort fallback shape.

    Not the real return type of ``LiveIntegration.advertised_objectives()``
    (see :func:`_composition_objectives`) - kept as a tolerant fallback for a
    caller that hands this projection a plain sequence of mappings or bare
    strings instead of an ``AdvertisedComposition``, so an unanticipated
    future shape still degrades to something rather than to ``UNKNOWN``
    outright.  A mapping with ``objective``/``executable``/``a1PolicyType``/
    ``reason`` keys is read first; a bare string is accepted as an executable
    objective with no further detail; anything else is skipped.
    """
    if isinstance(item, Mapping):
        objective = _first(item.get("objective"), item.get("kind"), item.get("name"))
        if not objective:
            return None
        return ObjectiveAdvertisementView(
            objective=str(objective), executable=bool(item.get("executable")),
            a1_policy_type=_first(item.get("a1PolicyType"), item.get("a1_policy_type")),
            reason=item.get("reason"))
    if isinstance(item, str) and item:
        return ObjectiveAdvertisementView(objective=item, executable=True)
    return None


def _composition_objectives(composition: Any) -> Optional[tuple[ObjectiveAdvertisementView, ...]]:
    """Read an ``AdvertisedComposition``-shaped object (``oran/integration/
    objectives.py``, frozen and owned elsewhere) by attribute, never by
    ``isinstance``: a ``.objectives`` tuple of objective advertisements
    (``.kind``/``.executable``/``.reason``) and a ``.extensions`` tuple of
    experimental-extension advertisements (``.name``/``.a1_policy_type``/
    ``.reason``, always non-executable).  Every attribute read is defensive,
    so a shape this projection does not recognise degrades to ``None`` -
    which the caller renders as ``UNKNOWN`` with a reason - rather than
    raising or silently reporting an empty, falsely-successful list.
    """
    objectives_attr = getattr(composition, "objectives", None)
    extensions_attr = getattr(composition, "extensions", None)
    if objectives_attr is None and extensions_attr is None:
        return None
    views = []
    for item in objectives_attr or ():
        kind = getattr(item, "kind", None)
        if not kind:
            continue
        views.append(ObjectiveAdvertisementView(
            objective=str(kind), executable=bool(getattr(item, "executable", False)),
            a1_policy_type=None, reason=getattr(item, "reason", None)))
    for item in extensions_attr or ():
        name = getattr(item, "name", None)
        if not name:
            continue
        views.append(ObjectiveAdvertisementView(
            objective=str(name), executable=bool(getattr(item, "executable", False)),
            a1_policy_type=getattr(item, "a1_policy_type", None),
            reason=getattr(item, "reason", None)))
    return tuple(views)


#: ``identity()`` spells the composed-release binding record with the neutral
#: key.  The retired spelling is still read, second, because the preserved
#: history-only integration entry (``oran/rapp/gui_entry.py``) emits it and
#: that file is not edited; a live producer must use the neutral key.
_BINDING_IDENTITY_KEYS = ("composedReleaseBinding", "lowerReleaseBinding")


def _composed_release_binding_label(value: Any) -> Optional[str]:
    """Render the composed-release binding record from ``identity()``.

    The real value is the binding record's full dict (``oran/integration/
    deployment_binding.py:DeploymentBindingContracts.binding()``), not a bare
    string; a plain string is still accepted so an earlier or simplified shape
    does not regress this to Unknown.  Any other shape, or a record with no
    ``release`` key, renders ``None`` (Unknown) rather than a guess.
    """
    if isinstance(value, str) and value:
        return value
    if isinstance(value, Mapping):
        release = value.get("release")
        if not release:
            return None
        tag = value.get("tag")
        commit = value.get("commit")
        label = str(release)
        if tag:
            label += f" (tag {tag})"
        if commit:
            label += f" @{str(commit)[:12]}"
        return label
    return None


def project_deployment_provenance(integration: Any) -> DeploymentProvenanceView:
    """Which deployed release the bound deployment composes against, honestly.

    ``integration`` is a ``LiveIntegration`` (``oran/rapp/gui_entry.py``,
    frozen and owned elsewhere).  The composed-release binding (see
    :data:`_BINDING_IDENTITY_KEYS`) and ``compositionBasis`` are read from its
    ``identity()`` dict, which already omits any key the deployment did not
    declare - so a missing key here renders ``None``/Unknown rather than a
    guessed value.
    ``advertised_objectives()`` is read defensively through ``getattr``: even
    though its real return type (``AdvertisedComposition``) is now known, a
    console built against an older or newer integration build may not expose
    the method, or may expose a different shape, and absence must report
    ``UNKNOWN`` with a stated reason - never an empty-but-successful
    objective list, which would read as "this deployment executes nothing"
    rather than "this console cannot yet tell".
    """
    if integration is None:
        return DeploymentProvenanceView(
            objectives_status="UNKNOWN",
            objectives_reason="no deployment is bound")
    try:
        identity = dict(integration.identity())
    except Exception:
        identity = {}
    composed_release_binding = _composed_release_binding_label(next(
        (identity[key] for key in _BINDING_IDENTITY_KEYS if key in identity),
        None))
    composition_basis = identity.get("compositionBasis")
    getter = getattr(integration, "advertised_objectives", None)
    if not callable(getter):
        return DeploymentProvenanceView(
            composed_release_binding=composed_release_binding,
            composition_basis=composition_basis, objectives_status="UNKNOWN",
            objectives_reason="advertised_objectives() is not available on "
                             "this integration build")
    try:
        raw = getter()
    except Exception as exc:
        return DeploymentProvenanceView(
            composed_release_binding=composed_release_binding,
            composition_basis=composition_basis, objectives_status="UNKNOWN",
            objectives_reason=f"advertised_objectives() raised "
                              f"{type(exc).__name__}: {exc}")
    objectives = _composition_objectives(raw)
    if objectives is None:
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return DeploymentProvenanceView(
                composed_release_binding=composed_release_binding,
                composition_basis=composition_basis, objectives_status="UNKNOWN",
                objectives_reason="advertised_objectives() returned an "
                                 "unrecognised shape")
        objectives = tuple(view for view in (_objective_view(item) for item in raw)
                           if view is not None)
    return DeploymentProvenanceView(
        composed_release_binding=composed_release_binding,
        composition_basis=composition_basis, objectives=objectives,
        objectives_status="OK", objectives_reason=None)


def submit_intent(submission: IntentSubmission, *, present: Optional[Callable[[Mapping[str, Any]], None]] = None,
                  runner: Optional[Callable[..., Mapping[str, Any]]] = None,
                  validate_intent: Optional[Callable[[Mapping[str, Any]],
                                                     Mapping[str, Any]]] = None,
                  **runner_kwargs: Any) -> IntentSubmissionResult:
    """Run one authoritative episode.  Intended for a cancellable worker.

    ``runner`` is required.  It used to default to importing the preserved
    episode entry, which made the pre-Kernel decision runtime reachable from
    inside the console package; the entry is now handed in by whoever composed
    the session, and a caller that has none is refused here rather than being
    silently given one.
    """
    if not submission.intent_text:
        raise ValueError("intentText must be a non-empty string")
    if runner is None:
        raise ValueError(
            "no episode runner was supplied; the console imports no episode "
            "runtime and cannot choose one for you")
    submitted_at = _utc_now()
    presented: list[Mapping[str, Any]] = []

    def presenter(value: Mapping[str, Any]) -> None:
        snapshot = deepcopy(dict(value))
        presented.append(snapshot)
        if present is not None:
            try:
                present(deepcopy(snapshot))
            except Exception:
                pass

    authoritative = runner(
        integration_path=submission.integration_path,
        request=deepcopy(dict(submission.request)), present=presenter,
        **runner_kwargs)
    authoritative_snapshot = deepcopy(dict(authoritative))
    decision = project_decision(authoritative_snapshot,
                                intent_text=submission.intent_text,
                                validate_intent=validate_intent)
    row = project_intent_row(
        authoritative_snapshot, intent_text=submission.intent_text,
        submitted_at=submitted_at, target_scope=submission.target_scope,
        priority=submission.priority, validity=submission.validity)
    return IntentSubmissionResult(
        authoritative=authoritative_snapshot, decision=decision, intent=row,
        submitted_at=submitted_at)


class IntentWorker:
    """Cancellable, daemon worker for one intent episode.

    Cancellation is cooperative: it prevents any late result from reaching the
    UI.  The authoritative coordinator call is allowed to settle safely rather
    than being forcefully interrupted in the middle of a policy lifecycle.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    def start(self, submission: IntentSubmission, *,
              on_result: Callable[[IntentSubmissionResult], None],
              on_error: Optional[Callable[[Exception], None]] = None,
              present: Optional[Callable[[Mapping[str, Any]], None]] = None,
              runner: Optional[Callable[..., Mapping[str, Any]]] = None,
              validate_intent: Optional[Callable[[Mapping[str, Any]],
                                                 Mapping[str, Any]]] = None,
              **runner_kwargs: Any) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("an intent episode is already running")
            self._cancelled = threading.Event()

            def work() -> None:
                try:
                    value = submit_intent(
                        submission, present=present, runner=runner,
                        validate_intent=validate_intent, **runner_kwargs)
                    if not self._cancelled.is_set():
                        on_result(value)
                except Exception as exc:
                    if not self._cancelled.is_set() and on_error is not None:
                        on_error(exc)

            self._thread = threading.Thread(
                target=work, name="operator-intent-worker", daemon=True)
            self._thread.start()

    def cancel(self) -> None:
        self._cancelled.set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Wait for settlement; return ``False`` only when still running."""
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()


def withdraw_intent(row: IntentRowView,
                    lifecycle: Callable[[IntentRowView], Any]) -> tuple[bool, IntentRowView, Optional[str]]:
    """Run an injected real lifecycle operation; never optimistically delete."""
    try:
        outcome = lifecycle(row)
    except Exception as exc:
        return False, row, str(exc)
    if isinstance(outcome, Mapping):
        ok = bool(outcome.get("success"))
        reason = outcome.get("error") or outcome.get("reason")
    else:
        ok, reason = bool(outcome), None
    if not ok:
        return False, row, str(reason or "Lifecycle operation was not accepted")
    return True, replace(row, lifecycle="WITHDRAWN", last_update_at=_utc_now()), None


__all__ = [
    "IntentSubmission", "IntentSubmissionResult", "IntentWorker", "POLICY_TYPE", "POLICY_VERSION",
    "build_llm_switch_confirmation", "build_submit_confirmation", "build_withdraw_confirmation",
    "normalized_intent_preview", "project_calibration", "project_correlation_trace",
    "project_decision", "project_deployment_provenance", "project_intent_row",
    "submit_intent", "withdraw_intent",
]
