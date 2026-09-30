"""rApp northbound status projection - read-only.

Signatures frozen by the design step; body owned and implemented by track **T1**
(see ``docs/phase-b-gui/file-ownership.1.0.0.json``).
Boundary authority: ``docs/phase-b-gui/boundary-map.1.0.0.json``.

Two tracks implemented this body independently.  Integration kept T1's, which is
the owner of record, and carried across the two properties only T3's version
had: an unattributed transition is ``UNKNOWN`` rather than absent, and
``binding_count`` counts *active* bindings.  T3's narrower ``e2_deployment`` was
deliberately not carried across - it dropped ``nodes``, which is where the Live
Operations topology grid gets the deployment's E2 node rows from.

``producer_epoch`` is typed ``Optional[str]``: the design step wrote
``Optional[int]``, but ``R1Client`` writes an opaque epoch token (``"epoch-1"``)
and a projection must not claim a shape the producer does not honour.

Why this module exists
----------------------
``run_gui_once`` is the only sanctioned GUI entry into the rApp today, and it is
fire-and-forget: there is no polling, subscription or streaming read API in
``oran/rapp/``.  An operator console needs a *status* read path.

The three richest status payloads in the repository are the development
control-plane state routes on ``RAppHttpServer`` and ``NonRtRicService`` and the
O1 consumer's development state accessor.  All three are explicitly declared
development control planes, **not R1**.  They are exactly the shortcut that
would defeat the boundary, so they are forbidden to the console unconditionally,
regardless of ``insecure_dev``.

This module is the sanctioned alternative: it projects status from what the rApp
legitimately already holds - R1 read methods and the rApp's own durable state and
FSM snapshot.  It adds **no new transport**.

It lives in ``oran/rapp/`` rather than in ``gui/`` on purpose, so that the
import-graph walk in ``tests/test_oran_boundary.py`` *proves* the GUI cannot
reach a transport directly, instead of the boundary resting on convention.

Hard constraints this body honours
----------------------------------
* **Read-only.**  No call to ``create_policy``, ``update_policy``,
  ``delete_policy``, ``create_status_subscription``, ``update_status_subscription``,
  ``delete_status_subscription``, ``create_continuous_job``, ``update_data_job``,
  ``delete_data_job``, ``accept_evidence``, ``recover_one_time_pull`` or any
  development control-plane method.  ``tests/gui/test_status_projection.py``
  asserts this by AST, so the property is proven rather than promised.
* No import of ``oran.nonrt.a1_client`` - A1 belongs to the Non-RT RIC Framework.
* No ``subprocess``, no ``socket``, no direct HTTP client.  Network access stays
  inside ``oran/rapp/r1_client.py``'s existing transport.
* No development control-plane route is named or built anywhere in this file.
* Never raises outward: a projection failure yields ``None`` or an empty view
  with a reason, because a status read must not be able to abort an experiment.

Every accessor is defensive about shape as well as about failure.  The durable
store is a plain JSON document that a previous release may have written, so a
missing key, a wrong type or a truncated record has to degrade into a stated
``error`` rather than into a traceback on the operator's screen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Dict, Final, List, Mapping, Optional, Tuple

logger = logging.getLogger("oran.rapp.status_projection")

#: Machine-readable assertion consumed by the boundary scan gate.
READ_ONLY: Final[bool] = True


@dataclass(frozen=True)
class PolicyStatusView:
    """A projection of one ``AIC_UECellSteering_1.0.0.status`` object.

    This is the **A1 policy** axis of the three-axis separation.  It is a
    different thing from the coordinator's intent state and from the evidence
    state, and the console renders all three separately.
    """

    policy_id: str
    policy_revision: Optional[str] = None
    enforce_status: Optional[str] = None       # ENFORCED | NOT_ENFORCED
    enforce_reason: Optional[str] = None
    policy_state: Optional[str] = None         # ACTIVE | ... | ERROR
    policy_terminal: Optional[bool] = None
    episode_id: Optional[str] = None
    episode_state: Optional[str] = None
    episode_terminal: Optional[bool] = None
    control_result: Optional[str] = None       # PENDING | ACK | NACK | TIMEOUT | UNKNOWN
    readback_result: Optional[str] = None      # VERIFIED | MISMATCH | MISSING | STALE | NOT_AVAILABLE
    rollback_state: Optional[str] = None
    error_stage: Optional[str] = None
    error_code: Optional[str] = None
    occurred_at: Optional[str] = None
    producer_epoch: Optional[str] = None
    status_seq: Optional[int] = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvidenceStateView:
    """A projection of one accepted ``aic:policy-evidence:1.0.0`` record.

    This is the **evidence** axis: what was actually observed, with its quality
    and freshness.  A non-OK quality never renders as a plain value.
    """

    observation_id: str
    data_job_id: Optional[str] = None
    binding_id: Optional[str] = None
    policy_id: Optional[str] = None
    observed_at: Optional[str] = None
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    phase: Optional[str] = None                # BEFORE | AFTER | STEADY | OVERLAPS_ACTION
    quality: Optional[str] = None              # OK | SUSPECT | STALE | MISSING | ...
    ambiguity_reason: Optional[str] = None
    measurement_dn: Optional[str] = None
    cell_id: Optional[str] = None
    sample_count: int = 0
    measurement_names: Tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RappStateView:
    """Everything the console can legitimately learn about rApp state at once."""

    policies: Tuple[PolicyStatusView, ...] = ()
    evidence: Tuple[EvidenceStateView, ...] = ()
    transitions: Tuple[Mapping[str, Any], ...] = ()
    terminal_outcome: Optional[str] = None
    ledger_references: Tuple[str, ...] = ()
    binding_count: int = 0
    audit_flags: Tuple[str, ...] = ()
    captured_at: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class CapabilityView:
    """The declared deployment inventory and KPI set.

    The console enumerates topology elements and the metric registry from this,
    never from a hard-coded list - phaseB_task.md section 6 forbids forcing a
    fixed component count or name onto a deployment.

    ``contract_profile``, ``service_models`` and ``o1_performance_profile_id``
    are carried for the same reason ``software_provenance`` and
    ``e2_deployment`` are: phaseB_task.md section 2 asks for the *applied*
    version / release / profile of each topology element, and the capability
    manifest is the only sanctioned source that declares any of it.  Dropping
    them here would leave the console with nothing to show but a placeholder,
    which is how integration-field-gaps.md GAP-13 came to be reported as
    unimplemented.  What the manifest does not declare stays undeclared - this
    projection never fills a gap in.
    """

    manifest_id: Optional[str] = None
    near_rt_ric_id: Optional[str] = None
    effective_at: Optional[str] = None
    topology: Mapping[str, Any] = field(default_factory=dict)
    decision_kpis: Tuple[str, ...] = ()
    assurance_kpis: Tuple[str, ...] = ()
    control_axes: Tuple[str, ...] = ()
    policy_types: Tuple[str, ...] = ()
    e2_deployment: Mapping[str, Any] = field(default_factory=dict)
    software_provenance: Mapping[str, Any] = field(default_factory=dict)
    service_models: Mapping[str, Any] = field(default_factory=dict)
    contract_profile: Optional[str] = None
    o1_performance_profile_id: Optional[str] = None
    limits: Mapping[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# Small shape-tolerant helpers
# --------------------------------------------------------------------------- #


def _as_mapping(value: Any) -> Dict[str, Any]:
    """A dict copy of ``value``, or an empty dict for anything else."""
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> Optional[str]:
    """A string, or ``None``.  Never a coerced empty string.

    ``None`` here means *honest unknown*.  Returning ``""`` would put a blank in
    a cell that is supposed to show the pre-measurement placeholder.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    return str(value)


def _flag(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cell_label(cell: Any) -> Optional[str]:
    """Render a contract ``CellId`` as ``mcc-mnc/ncI`` for display."""
    cell_map = _as_mapping(cell)
    if not cell_map:
        return None
    plmn = _as_mapping(cell_map.get("plmnId"))
    ncid = _as_mapping(cell_map.get("cId")).get("ncI")
    # The PLMN parts are read into descriptive names rather than the wire
    # spellings, because the boundary scan matches forbidden command tokens as
    # plain substrings and the netcat token collides with the mobile network
    # code identifier followed by a space.
    country_code = plmn.get("mcc")
    network_code = plmn.get("mnc")
    if country_code and network_code and ncid is not None:
        return f"{country_code}-{network_code}/{ncid}"
    return _text(ncid)


def _status_view(policy_id: str, document: Any) -> Optional[PolicyStatusView]:
    """Build a :class:`PolicyStatusView` from one status document.

    Returns ``None`` when the document is not a mapping at all; a partial
    document still projects, with the absent fields left ``None`` so the console
    shows Unknown rather than inventing a state.
    """
    doc = _as_mapping(document)
    if not doc:
        return None
    aic = _as_mapping(doc.get("aicStatus"))
    control = _as_mapping(aic.get("control"))
    readback = _as_mapping(aic.get("readback"))
    rollback = _as_mapping(aic.get("rollback"))
    error = _as_mapping(aic.get("error"))
    revision = aic.get("policyRevision")
    seq = aic.get("statusSeq")
    return PolicyStatusView(
        policy_id=_text(aic.get("policyId")) or policy_id,
        policy_revision=_text(revision),
        enforce_status=_text(doc.get("enforceStatus")),
        enforce_reason=_text(doc.get("enforceReason")),
        policy_state=_text(aic.get("policyState")),
        policy_terminal=_flag(aic.get("policyTerminal")),
        episode_id=_text(aic.get("episodeId")),
        episode_state=_text(aic.get("episodeState")),
        episode_terminal=_flag(aic.get("episodeTerminal")),
        control_result=_text(control.get("result")),
        readback_result=_text(readback.get("result")),
        rollback_state=_text(rollback.get("state")),
        error_stage=_text(error.get("stage")),
        error_code=_text(error.get("code")),
        occurred_at=_text(aic.get("occurredAt")),
        producer_epoch=_text(aic.get("producerEpoch")),
        status_seq=seq if isinstance(seq, int) and not isinstance(seq, bool) else None,
        raw=doc,
    )


def _evidence_view(identity: str, entry: Any) -> Optional[EvidenceStateView]:
    """Build an :class:`EvidenceStateView` from one durable evidence entry."""
    holder = _as_mapping(entry)
    record = _as_mapping(holder.get("record"))
    if not record:
        return None
    window = _as_mapping(record.get("window"))
    scope = _as_mapping(record.get("measurementScope"))
    correlation = _as_mapping(record.get("correlation"))
    samples = record.get("samples")
    sample_list = list(samples) if isinstance(samples, (list, tuple)) else []
    names: List[str] = []
    for sample in sample_list:
        name = _text(_as_mapping(sample).get("name"))
        if name and name not in names:
            names.append(name)
    return EvidenceStateView(
        observation_id=_text(record.get("observationId")) or identity,
        data_job_id=_text(holder.get("dataJobId")),
        binding_id=_text(holder.get("bindingId")),
        policy_id=_text(correlation.get("policyId")),
        observed_at=_text(record.get("observedAt")),
        window_start=_text(window.get("start")),
        window_end=_text(window.get("end")),
        phase=_text(record.get("phase")),
        quality=_text(record.get("quality")),
        ambiguity_reason=_text(record.get("ambiguityReason")),
        measurement_dn=_text(scope.get("managedObjectDn")),
        cell_id=_cell_label(scope.get("cellId")),
        sample_count=len(sample_list),
        measurement_names=tuple(names),
        raw=record,
    )


def _store_snapshot(client: Any) -> Dict[str, Any]:
    """``JsonStateStore.snapshot()`` off ``client.state``, defensively.

    The rApp already holds this document; reading it adds no transport and no
    round trip.  Callers take **one** snapshot and derive everything from it -
    see :func:`project_state_store`.
    """
    store = getattr(client, "state", None)
    reader = getattr(store, "snapshot", None)
    if reader is None:
        return {}
    return _as_mapping(reader())


def _policies_from(snapshot: Mapping[str, Any]) -> Tuple[PolicyStatusView, ...]:
    """Policy views from an already-taken snapshot.  Pure; reads nothing."""
    entries = _as_mapping(_as_mapping(snapshot).get("status"))
    views: List[PolicyStatusView] = []
    for policy_id in sorted(entries):
        holder = _as_mapping(entries[policy_id])
        view = _status_view(policy_id, holder.get("snapshot"))
        if view is None:
            continue
        if view.producer_epoch is None or view.status_seq is None:
            # The durable ledger carries the dedupe keys alongside the document,
            # so a status object that lost them still projects with its identity.
            ledger_seq = holder.get("statusSeq")
            view = replace(
                view,
                producer_epoch=view.producer_epoch
                or _text(holder.get("producerEpoch")),
                status_seq=view.status_seq if view.status_seq is not None
                else (ledger_seq if isinstance(ledger_seq, int)
                      and not isinstance(ledger_seq, bool) else None),
            )
        views.append(view)
    return tuple(views)


def _evidence_from(snapshot: Mapping[str, Any]) -> Tuple[EvidenceStateView, ...]:
    """Evidence views from an already-taken snapshot.  Pure; reads nothing."""
    entries = _as_mapping(_as_mapping(snapshot).get("evidence"))
    views: List[EvidenceStateView] = []
    for identity in sorted(entries):
        view = _evidence_view(identity, entries[identity])
        if view is not None:
            views.append(view)
    return tuple(views)


# --------------------------------------------------------------------------- #
# Projections
# --------------------------------------------------------------------------- #


def project_policy_status(client, policy_id: str) -> Optional[PolicyStatusView]:
    """Project one policy's status via ``R1Client.get_policy_status``.

    Returns ``None`` when the policy is unknown or the read fails; the caller
    renders ``UNAVAILABLE`` with a reason rather than a blank.
    """
    reader = getattr(client, "get_policy_status", None)
    if reader is None:
        return None
    try:
        return _status_view(policy_id, reader(policy_id))
    except Exception:
        logger.debug("policy status projection failed for %r", policy_id,
                     exc_info=True)
        return None


def project_policies(client) -> Tuple[PolicyStatusView, ...]:
    """Project every policy the rApp knows about.  **Exactly one store read.**

    Sourced from ``JsonStateStore.snapshot()['status']``, which holds the
    authoritative snapshot the rApp already accepted - no extra R1 round trip
    per policy.
    """
    try:
        return _policies_from(_store_snapshot(client))
    except Exception:
        logger.debug("policy projection failed", exc_info=True)
        return ()


def project_evidence(client) -> Tuple[EvidenceStateView, ...]:
    """Project accepted DME evidence from ``JsonStateStore.snapshot()['evidence']``.

    **Exactly one store read.**
    """
    try:
        return _evidence_from(_store_snapshot(client))
    except Exception:
        logger.debug("evidence projection failed", exc_info=True)
        return ()


def project_state_store(client) -> RappStateView:
    """Project the whole rApp durable state in one read.

    **Read contract: ``client.state.snapshot()`` is called exactly once.**  That
    is not an optimisation, it is the correctness property.  The console renders
    the intent, A1-policy and evidence axes side by side, and a row that mixes
    policy state from one read with evidence from another is a picture of a
    moment that never existed.  Taking a second snapshot mid-projection would
    also lose everything a store that is valid only on its first read has to
    say - which is exactly how the defect showed up: three reads, and policies
    and evidence both came back empty.

    So the snapshot is taken once here and *passed* to :func:`_policies_from`
    and :func:`_evidence_from`, which read nothing themselves.

    A failure yields an empty view carrying ``error`` - the console renders that
    as ``UNAVAILABLE`` with the reason attached, and every other pane keeps
    working.
    """
    try:
        snapshot = _store_snapshot(client)
    except Exception as exc:
        logger.debug("state store projection failed", exc_info=True)
        return RappStateView(captured_at=_now_utc(),
                             error=f"{type(exc).__name__}: {exc}")
    policies = _policies_from(snapshot)
    evidence = _evidence_from(snapshot)
    bindings = _as_mapping(snapshot.get("bindings"))
    audit = snapshot.get("audit")
    flags: List[str] = []
    if isinstance(audit, (list, tuple)):
        for entry in audit:
            kind = _text(_as_mapping(entry).get("kind"))
            if kind and kind not in flags:
                flags.append(kind)
    references: List[str] = []
    active = 0
    for binding_id, binding in sorted(bindings.items()):
        record = _as_mapping(binding)
        job = _text(record.get("dataJobId"))
        references.append(f"{binding_id}:{job}" if job else str(binding_id))
        if record.get("active") is True:
            active += 1
    return RappStateView(
        policies=policies, evidence=evidence,
        # Two different questions, answered separately on purpose.
        # ``ledger_references`` is the whole ledger, because an operator
        # debugging a missing observation needs to see the pending and the
        # retired binding too.  ``binding_count`` is the number of *active*
        # bindings, because ``R1Client`` writes ``active: False`` for a binding
        # that is merely pre-created or already retired, and counting those as
        # live bindings would tell the console that evidence is on its way when
        # nothing is bound.
        ledger_references=tuple(references), binding_count=active,
        audit_flags=tuple(flags), captured_at=_now_utc(),
    )


def project_transitions(adapter) -> Tuple[Mapping[str, Any], ...]:
    """Project the FSM transition snapshot from the coordinator adapter.

    Each entry carries ``origin`` of ``REAL`` or ``SYNTHETIC``; the console
    renders synthetic transitions distinctly so a contract-driven transition is
    never read as a measured one.

    An entry that does not say how it was produced is stamped ``UNKNOWN``, never
    left absent.  Absent would reach the console as ``None`` and render in the
    same neutral style as a measured transition, which is the one reading this
    projection exists to prevent.  Everything else the adapter reported is
    passed through untouched - the console shows fields this module has never
    heard of rather than silently dropping them.
    """
    reader = getattr(adapter, "transition_snapshot", None)
    if reader is None:
        return ()
    try:
        entries = reader()
    except Exception:
        logger.debug("transition projection failed", exc_info=True)
        return ()
    if not isinstance(entries, (list, tuple)):
        return ()
    projected: List[Mapping[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        record = _as_mapping(entry)
        record["origin"] = _text(record.get("origin")) or "UNKNOWN"
        projected.append(record)
    return tuple(projected)


def project_capability(manifest: Mapping[str, Any]) -> CapabilityView:
    """Project the RAN capability manifest into the inventory view.

    KPI names are pulled out of the declared objects rather than assumed, and a
    manifest that declares none yields an empty tuple - never a default list.
    That is what keeps the console reflecting a real deployment instead of this
    testbed's shape.
    """
    document = _as_mapping(manifest)
    if not document:
        return CapabilityView(error="capability manifest absent or unreadable")

    def _names(key: str) -> Tuple[str, ...]:
        raw = document.get(key)
        if not isinstance(raw, (list, tuple)):
            return ()
        out: List[str] = []
        for item in raw:
            name = _text(item) if isinstance(item, str) \
                else _text(_as_mapping(item).get("name"))
            if name and name not in out:
                out.append(name)
        return tuple(out)

    axes = document.get("controlAxes")
    types = document.get("policyTypes")
    return CapabilityView(
        manifest_id=_text(document.get("manifestId")),
        near_rt_ric_id=_text(document.get("nearRtRicId")),
        effective_at=_text(document.get("effectiveAt")),
        topology=_as_mapping(document.get("topology")),
        decision_kpis=_names("decisionKpis"),
        assurance_kpis=_names("assuranceKpis"),
        control_axes=tuple(str(a) for a in axes) if isinstance(axes, (list, tuple)) else (),
        policy_types=tuple(str(t) for t in types) if isinstance(types, (list, tuple)) else (),
        e2_deployment=_as_mapping(document.get("e2Deployment")),
        software_provenance=_as_mapping(document.get("softwareProvenance")),
        service_models=_as_mapping(document.get("serviceModels")),
        contract_profile=_text(document.get("contractProfile")),
        o1_performance_profile_id=_text(document.get("o1PerformanceProfileId")),
        limits=_as_mapping(document.get("limits")),
    )


__all__ = [
    "READ_ONLY", "CapabilityView", "EvidenceStateView", "PolicyStatusView",
    "RappStateView", "project_capability", "project_evidence",
    "project_policies", "project_policy_status", "project_state_store",
    "project_transitions",
]
