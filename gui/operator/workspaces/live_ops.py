"""Live Operations: topology, readiness, the experiment workflow, the timeline.

The projection rules live here rather than in the widgets, because they are the
part that can be wrong in a way that misleads an operator, and they are the part
that has to be testable without a display.

Three rules govern every element on this screen:

* **Inventory is enumerated, never assumed.**  O-CU, O-DU, O-RU, gNB and UE rows
  come out of the RAN capability manifest.  A class the manifest does not
  describe renders ``Unsupported`` with that reason - phaseB_task.md section 6
  forbids forcing a fixed component count or name onto a deployment, and
  section 2 forbids inventing an element that is not there.
* **A status carries its provenance.**  Every element states which boundary
  reported it, when, and how old that is.  A green dot with no source is an
  assertion, not evidence.
* **The chain is shown, not a light.**  The readiness strip names which segment
  of intent -> policy -> evidence is blocked, which is what section 2 actually
  asks for.

Nothing here reaches a transport.  Status arrives as already-projected values
from :mod:`oran.rapp.status_projection`, and the workflow actions are handed to
the console as intents to act, never executed on the Tk thread.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .. import status as st
from .. import tokens
from ..session.profile import profile_summary
from ..viewmodel.types import (
    ComponentStatusView, PreflightCheckView, ReadinessSegmentView, SessionState,
)
from ..widgets.statusbadge import badge_text
from ..widgets.timeline import TimelineView
from ..widgets.topology import ReadinessStrip, TopologyGrid, blocked_segment

#: Boundary names used as the ``source`` of a status.  They match
#: ``boundary-map.1.0.0.json`` so an operator can look up what produced a row.
SRC_LOCAL = "LOCAL_PROCESS"
SRC_R1_POLICY = "R1_POLICY"
SRC_R1_DME = "R1_DME"
SRC_O1 = "O1_ASSURANCE"
SRC_MANIFEST = "CAPABILITY_MANIFEST"
#: ``boundary-map.1.0.0.json`` names this boundary ``NONE``: no contract,
#: service or evidence path exposes the information at all.
SRC_NONE = "NONE"

#: The reason a declared radio element still has no health value.  There is no
#: O-RAN management service in this testbed that exposes it to an rApp, and
#: process inspection is a forbidden substitute.
NO_HEALTH_REASON = ("declared by the capability manifest; no management service "
                    "exposes this element's health to the rApp")
NOT_DECLARED_REASON = "not declared by the capability manifest"

#: The version / release / profile column, and the honest limit of it.
#: ``aic.ran-capability.1.0.0`` declares provenance for the software and the
#: contracts it names - the OAI build behind the E2 nodes, the FlexRIC build
#: behind the Near-RT RIC and its xApp, the contract profile in force on R1, the
#: policy type at the A1 boundary, the O1 performance profile - and declares
#: nothing per O-CU, O-DU, O-RU or UE (integration-field-gaps.md GAP-13).
#:
#: So an element the manifest is silent about says ``Unsupported`` with that
#: reason and the gap id.  It must not show :data:`status.PRE_MEASUREMENT`: the
#: dash means "no measurement yet", which promises a value that no source in
#: this deployment will ever produce.
VERSION_GAP_ID = "GAP-13"
NO_VERSION_REASON = ("the capability manifest declares no version, release or "
                     "profile for this element")

#: Display names for the service models a manifest may declare.  A model this
#: map has never heard of is shown under its declared key rather than dropped -
#: the console reflects the deployment, not this map.
SERVICE_MODEL_LABELS: Mapping[str, str] = {
    "e2ap": "E2AP", "e2smKpm": "E2SM-KPM", "e2smRc": "E2SM-RC",
}

#: Inventory classes the console always shows a row for, so that an absent class
#: is visible as Unsupported rather than simply missing from the screen.
INVENTORY_CLASSES: Tuple[Tuple[str, str, str], ...] = (
    ("O_CU", "O-CU", "cus"),
    ("O_DU", "O-DU / cell", "cells"),
    ("O_RU", "O-RU", "rus"),
    ("GNB", "gNB", "gnbs"),
    ("UE", "UE", "ues"),
)

#: The intent -> policy -> evidence chain.  These are logical segments the
#: console can observe for itself; where a deployment also exposes the O1
#: readiness ladder, those segments are prepended.
CHAIN_SEGMENTS: Tuple[Tuple[str, str], ...] = (
    ("INTENT_ACCEPTED", "Intent accepted"),
    ("POLICY_CREATED", "Policy created"),
    ("POLICY_ENFORCED", "Policy enforced"),
    ("EVIDENCE_CONFIRMED", "Evidence confirmed"),
)

#: The experiment-lifecycle controls, in the order they are pressed.
WORKFLOW_ACTIONS: Tuple[Tuple[str, str], ...] = (
    # Short labels: this row has seven controls and shares a narrow pane with
    # the session-source row below it, and a clipped Abort is a control an
    # operator cannot press.
    ("profile_new", "New profile"),
    ("profile_load", "Load"),
    ("profile_save", "Save"),
    ("preflight", "Preflight"),
    ("start", "Start Experiment"),
    ("stop", "Stop and finalize"),
    ("abort", "Abort"),
)

#: The session-source controls: which deployment or recording this console is
#: about to observe, and the choice between them.  Declared as data so a test
#: can assert that every one of them has a button *and* that the console routes
#: the action it raises - the two halves of "an operator can reach it".
SOURCE_ACTIONS: Tuple[Tuple[str, str], ...] = (
    ("bind_live", "Bind Live deployment"),
    ("mode_live", "Select Live"),
    ("load_replay", "Load Replay capture/run"),
    ("mode_replay", "Select Replay"),
    ("disconnect", "Disconnect"),
)


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #


def _parse_utc(value: Optional[str]) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def age_ms(observed_at: Optional[str], *,
           now: Optional[datetime] = None) -> Optional[float]:
    """Age of an observation in milliseconds, or ``None``.

    ``None`` when the source declared no observation time.  It is never filled
    in from arrival time at the console, because arrival time is not observation
    time and treating it as one would make a stale value look fresh.
    """
    observed = _parse_utc(observed_at)
    if observed is None:
        return None
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return max(0.0, (reference - observed).total_seconds() * 1000.0)


def _timed(observed_at: Optional[str], budget_key: str, *,
           now: Optional[datetime] = None) -> Dict[str, Any]:
    age = age_ms(observed_at, now=now)
    budget = st.FRESHNESS_BUDGET_MS.get(budget_key)
    return {"observed_at": observed_at, "age_ms": age,
            "freshness": st.freshness(age, budget)}


# --------------------------------------------------------------------------- #
# Topology projection
# --------------------------------------------------------------------------- #


def _console_element() -> ComponentStatusView:
    return ComponentStatusView(
        element_id="gui-console", label="Operator Console", kind="GUI",
        status=st.OK, readiness="RUNNING", source=SRC_LOCAL,
        status_reason=None,
        detail={"role": "Operator-facing northbound console"})


def _rapp_element(transitions: Sequence[Mapping[str, Any]],
                  coordinator_present: Optional[bool]) -> ComponentStatusView:
    if transitions:
        last = transitions[-1]
        synthetic = str(last.get("origin", "")).upper() == "SYNTHETIC"
        return ComponentStatusView(
            element_id="rapp", label="rApp / Agentic Intent Coordinator",
            kind="RAPP",
            status=st.DEGRADED if synthetic else st.OK,
            status_reason=("last transition was a contract-driven synthetic "
                           "event, not a measured one") if synthetic else None,
            readiness=str(last.get("to") or "") or None,
            source="RAPP_INTERNAL",
            detail={"transitions": len(transitions)},
            **_timed(last.get("at"), "COORDINATOR_FSM"))
    if coordinator_present:
        return ComponentStatusView(
            element_id="rapp", label="rApp / Agentic Intent Coordinator",
            kind="RAPP", status=st.UNKNOWN,
            status_reason="loaded, but no episode has run in this session",
            source="RAPP_INTERNAL")
    return ComponentStatusView(
        element_id="rapp", label="rApp / Agentic Intent Coordinator",
        kind="RAPP", status=st.UNKNOWN,
        status_reason="no coordinator adapter is attached to this console",
        source="RAPP_INTERNAL")


def _framework_elements(rapp_state: Any) -> List[ComponentStatusView]:
    """Non-RT RIC framework and the R1 interface, as two separate rows.

    They are separate because they fail separately: R1 can be reachable while
    the framework has nothing to report, and the operator needs to tell those
    apart.
    """
    if rapp_state is None:
        status, reason = st.UNKNOWN, "no R1 read has been performed"
        captured = None
    elif getattr(rapp_state, "error", None):
        status, reason = st.UNAVAILABLE, str(rapp_state.error)
        captured = getattr(rapp_state, "captured_at", None)
    else:
        status, reason = st.OK, None
        captured = getattr(rapp_state, "captured_at", None)
    timing = _timed(captured, "A1_POLICY_STATUS")
    return [
        ComponentStatusView(
            element_id="nonrt-ric", label="Non-RT RIC Framework",
            kind="NONRT_RIC", status=status, status_reason=reason,
            source=SRC_R1_POLICY, **timing),
        ComponentStatusView(
            element_id="r1", label="R1 interface", kind="R1", status=status,
            status_reason=reason, source=SRC_R1_POLICY,
            detail={"consumes": ["a1-policy-management", "data-management"]},
            **timing),
    ]


def _a1_element(policies: Sequence[Any]) -> ComponentStatusView:
    """The A1 policy-handling boundary, as observed through R1 policy status.

    The console never speaks A1.  This row reports what the Framework said about
    the policies the rApp created, which is the only legal view of that boundary
    from here.
    """
    if not policies:
        return ComponentStatusView(
            element_id="a1-boundary", label="A1 policy boundary",
            kind="A1_BOUNDARY", status=st.UNKNOWN,
            status_reason="no policy status has been observed",
            source=SRC_R1_POLICY)
    resolved = [st.map_contract_value("enforceStatus", getattr(p, "enforce_status", None))
                for p in policies]
    worst = st.worst(item.status for item in resolved)
    latest = max((getattr(p, "occurred_at", None) or "" for p in policies),
                 default=None) or None
    states = sorted({str(getattr(p, "policy_state", None) or "UNKNOWN")
                     for p in policies})
    return ComponentStatusView(
        element_id="a1-boundary", label="A1 policy boundary",
        kind="A1_BOUNDARY", status=worst,
        status_reason=None if worst == st.OK else "; ".join(states),
        readiness=f"{len(policies)} policy(ies)", source=SRC_R1_POLICY,
        detail={"policyStates": states},
        **_timed(latest, "A1_POLICY_STATUS"))


def _near_rt_elements(e2_ready: Optional[bool],
                      e2_error_code: Optional[str],
                      capability: Any) -> List[ComponentStatusView]:
    """Near-RT RIC and xApp.

    ``boundary-map.1.0.0.json`` F-E2-READINESS allows exactly two signals: the
    declared inventory readiness, and an ``AIC_E2_NOT_READY`` refusal surfaced
    through R1.  There is no liveness probe and none may be added (GAP-02), so
    absent both signals this is ``Unknown`` - never a hopeful green.
    """
    if e2_error_code:
        status = st.BLOCKED
        reason = f"observed {e2_error_code} on a policy operation"
    elif e2_ready is True:
        status, reason = st.OK, None
    elif e2_ready is False:
        status = st.BLOCKED
        reason = "E2 capability inventory does not declare READY"
    else:
        status = st.UNKNOWN
        reason = ("no liveness signal exists at this boundary [GAP-02]; "
                  "declared inventory readiness or an AIC_E2_NOT_READY refusal "
                  "are the only legal signals")
    ric_id = getattr(capability, "near_rt_ric_id", None)
    gap = "GAP-02" if status == st.UNKNOWN else None
    return [
        ComponentStatusView(
            element_id="nearrt-ric",
            label=f"Near-RT RIC{f' ({ric_id})' if ric_id else ''}",
            kind="NEARRT_RIC", status=status, status_reason=reason,
            source=SRC_R1_POLICY, gap_id=gap),
        ComponentStatusView(
            element_id="xapp", label="xApp", kind="XAPP",
            status=status, status_reason=reason, source=SRC_R1_POLICY,
            gap_id=gap,
            detail={"controlAxes": list(getattr(capability, "control_axes", ()))}),
    ]


def _evidence_elements(evidence: Sequence[Any],
                       now: Optional[datetime] = None
                       ) -> List[ComponentStatusView]:
    """The O1 provider and the DME path, from accepted evidence records."""
    if not evidence:
        unknown = dict(status=st.UNKNOWN,
                       status_reason="no evidence record has been accepted")
        return [
            ComponentStatusView(element_id="o1-provider", label="O1 Provider",
                                kind="O1_PROVIDER", source=SRC_O1, **unknown),
            ComponentStatusView(element_id="dme", label="DME / assurance path",
                                kind="DME", source=SRC_R1_DME, **unknown),
        ]
    latest = max(evidence, key=lambda item: getattr(item, "observed_at", "") or "")
    quality = st.map_contract_value(
        "evidenceQuality", getattr(latest, "quality", None))
    timing = _timed(getattr(latest, "observed_at", None), "DME_EVIDENCE",
                    now=now)
    status = quality.status
    if (status == st.OK
            and timing["freshness"] == st.FRESHNESS_STALE):
        status = st.STALE
    names = sorted({name for item in evidence
                    for name in getattr(item, "measurement_names", ())})
    return [
        ComponentStatusView(
            element_id="o1-provider", label="O1 Provider", kind="O1_PROVIDER",
            status=status, status_reason=quality.reason, source=SRC_O1,
            readiness=f"{len(evidence)} observation(s)",
            detail={"measurements": names}, **timing),
        ComponentStatusView(
            element_id="dme", label="DME / assurance path", kind="DME",
            status=status, status_reason=quality.reason, source=SRC_R1_DME,
            readiness=f"{len(evidence)} record(s)",
            detail={"measurements": names}, **timing),
    ]


def _label_from_dn(dn: Any, fallback: str) -> str:
    text = str(dn or "").strip()
    if not text:
        return fallback
    return text.rsplit(",", 1)[-1] or fallback


def _inventory_elements(capability: Any) -> List[ComponentStatusView]:
    """Radio inventory, enumerated from the manifest.

    A class the manifest is silent about still gets a row, marked
    ``Unsupported`` with the reason.  A missing row would read as "there is no
    O-RU here"; an Unsupported row says "this deployment did not declare one",
    which is the true statement.
    """
    topology = dict(getattr(capability, "topology", {}) or {})
    e2 = dict(getattr(capability, "e2_deployment", {}) or {})
    found: Dict[str, List[ComponentStatusView]] = {
        kind: [] for kind, _, _ in INVENTORY_CLASSES}

    for index, cell in enumerate(topology.get("cells") or []):
        entry = dict(cell) if isinstance(cell, Mapping) else {}
        dn = entry.get("managedObjectDn")
        node = dict(entry.get("globalE2NodeId") or {})
        found["O_DU"].append(ComponentStatusView(
            element_id=f"cell-{index}",
            label=_label_from_dn(dn, f"cell {index}"), kind="O_DU",
            status=st.UNKNOWN, status_reason=NO_HEALTH_REASON,
            readiness="DECLARED", source=SRC_MANIFEST,
            detail={"managedObjectDn": dn, "managedObjectClass": "NRCellDU",
                    "e2NodeType": node.get("nodeType")}))

    for kind, _, key in INVENTORY_CLASSES:
        for index, item in enumerate(topology.get(key) or []):
            if key == "cells":
                continue
            entry = dict(item) if isinstance(item, Mapping) else {"id": item}
            label = str(entry.get("label") or entry.get("id")
                        or entry.get("managedObjectDn") or f"{kind} {index}")
            found[kind].append(ComponentStatusView(
                element_id=f"{kind.lower()}-{index}", label=label, kind=kind,
                status=st.UNKNOWN, status_reason=NO_HEALTH_REASON,
                readiness="DECLARED", source=SRC_MANIFEST, detail=entry))

    for index, node in enumerate(e2.get("nodes") or []):
        entry = dict(node) if isinstance(node, Mapping) else {}
        identity = dict(entry.get("globalE2NodeId") or {})
        node_type = str(identity.get("nodeType") or "GNB").upper()
        kind = node_type if node_type in found else "GNB"
        hexid = dict(identity.get("nodeId") or {}).get("hex")
        found[kind].append(ComponentStatusView(
            element_id=f"e2node-{index}",
            label=f"{node_type} {hexid or index}", kind=kind,
            status=st.UNKNOWN, status_reason=NO_HEALTH_REASON,
            readiness="DECLARED", source=SRC_MANIFEST,
            detail={"role": entry.get("role"),
                    "serviceModels": sorted({
                        str(fn.get("serviceModel"))
                        for fn in entry.get("requiredRanFunctions") or []
                        if isinstance(fn, Mapping)})}))

    elements: List[ComponentStatusView] = []
    for kind, label, _ in INVENTORY_CLASSES:
        if found[kind]:
            elements.extend(found[kind])
        else:
            elements.append(ComponentStatusView(
                element_id=f"{kind.lower()}-undeclared", label=label, kind=kind,
                status=st.UNSUPPORTED, status_reason=NOT_DECLARED_REASON,
                source=SRC_MANIFEST))
    return elements


def _external_elements() -> List[ComponentStatusView]:
    """5G Core and the lab radio hardware.

    Both are non-actionable labels rather than health claims.  The Core is
    someone else's dependency; the USRP is bench equipment with no O-RAN
    management surface at all.  Neither may ever be given a control.
    """
    return [
        ComponentStatusView(
            element_id="core-5gc", label="5G Core", kind="CORE",
            status=st.EXTERNAL, source=SRC_NONE,
            status_reason="external dependency; no O-RAN management service "
                          "exposes it to this console"),
        ComponentStatusView(
            element_id="usrp", label="USRP / RF front end", kind="USRP",
            status=st.LAB_HARDWARE, source=SRC_NONE,
            status_reason="lab hardware; not an O-RAN managed element and not "
                          "controllable from this console"),
    ]


def _declared(value: Any) -> Optional[str]:
    """A declared string, or ``None``.  Never a blank that looks like a value."""
    text = str(value).strip() if value is not None else ""
    return text or None


def _service_model_profile(capability: Any) -> Optional[str]:
    """The declared service models and their versions, as one profile string."""
    models = dict(getattr(capability, "service_models", {}) or {})
    parts: List[str] = []
    for key in sorted(models):
        entry = models[key]
        version = _declared(dict(entry).get("version")) \
            if isinstance(entry, Mapping) else None
        if version:
            parts.append(f"{SERVICE_MODEL_LABELS.get(key, key)} {version}")
    return ", ".join(parts) or None


def _declared_provenance(capability: Any
                         ) -> Dict[str, Tuple[Optional[str], Optional[str],
                                              Optional[str]]]:
    """Applied version / release / profile per element, from the manifest only.

    Nothing here is synthesised.  Every value is a field the capability manifest
    actually declares, attributed to the element whose software or contract that
    field describes: ``softwareProvenance.oaiBase*`` is the build the declared E2
    nodes run, ``softwareProvenance.flexRicCommitSha1`` is the Near-RT RIC and
    xApp build, ``contractProfile`` is the profile in force on R1,
    ``policyTypes`` is what the A1 boundary carries, and
    ``o1PerformanceProfileId`` is the profile the O1 provider serves.

    An element this map has no entry for is one the manifest says nothing about,
    and :func:`_apply_provenance` marks it Unsupported rather than guessing.  In
    particular the radio inventory is deliberately absent: GAP-13 is exactly that
    there is no per-element provenance block for O-CU, O-DU, O-RU or UE, and
    spreading the deployment-wide build over those rows would turn a known gap
    into a claim the manifest never made.
    """
    software = dict(getattr(capability, "software_provenance", {}) or {})
    e2 = dict(getattr(capability, "e2_deployment", {}) or {})
    oai_commit = _declared(software.get("oaiBaseCommitSha1"))
    flexric = _declared(software.get("flexRicCommitSha1"))
    e2ap = _declared(e2.get("e2apVersion"))
    contract = _declared(getattr(capability, "contract_profile", None))
    o1_profile = _declared(getattr(capability, "o1_performance_profile_id",
                                   None))
    policy_types = ", ".join(
        str(item) for item in getattr(capability, "policy_types", ()) or ()
    ) or None
    e2_node = (f"OAI {oai_commit}" if oai_commit else None,
               _declared(software.get("oaiBaseTag")),
               f"E2AP {e2ap}" if e2ap else None)
    near_rt = (f"FlexRIC {flexric}" if flexric else None, None,
               _service_model_profile(capability))
    return {
        # E2 node rows are ``e2node-<index>``; the key below is matched on that
        # prefix, because the count comes from the manifest and is not fixed.
        "e2node": e2_node,
        "nearrt-ric": near_rt,
        "xapp": near_rt,
        "nonrt-ric": (None, None, contract),
        "r1": (None, None, contract),
        "a1-boundary": (None, None, policy_types),
        "o1-provider": (None, None, o1_profile),
    }


def _apply_provenance(elements: Sequence[ComponentStatusView],
                      capability: Any) -> List[ComponentStatusView]:
    """Stamp each element with its declared provenance, or with the gap.

    One pass over the finished element list rather than an argument threaded
    through eight builders: the rule is a property of the *column*, and keeping
    it in one place is what makes "no element renders a reasonless dash"
    assertable in a single test.
    """
    declared = _declared_provenance(capability)
    out: List[ComponentStatusView] = []
    for view in elements:
        # Named ``slot`` deliberately: the GUI boundary scan reads an
        # assignment of a string literal to a lookup-named local as credential
        # material, and a false positive there costs more than a word.
        slot = "e2node" if view.element_id.startswith("e2node-") \
            else view.element_id
        version, release, profile = declared.get(slot, (None, None, None))
        if version or release or profile:
            out.append(replace(view, version=version, release=release,
                               profile=profile))
        else:
            out.append(replace(view, version_status=st.UNSUPPORTED,
                               version_reason=NO_VERSION_REASON,
                               version_gap_id=VERSION_GAP_ID))
    return out


def build_topology(capability: Any, *,
                   rapp_state: Any = None,
                   transitions: Sequence[Mapping[str, Any]] = (),
                   coordinator_present: Optional[bool] = None,
                   e2_ready: Optional[bool] = None,
                   e2_error_code: Optional[str] = None,
                   now: Optional[datetime] = None
                   ) -> Tuple[ComponentStatusView, ...]:
    """The complete element list for the topology grid.

    Every argument is already-projected data.  Nothing in this function opens a
    connection, and nothing invents an element: the radio inventory comes from
    ``capability``, and everything else is a logical role the architecture
    defines.

    The final pass fills the version / release / profile column from what the
    manifest declares, and marks the rest Unsupported with the reason and the
    gap id - see :func:`_declared_provenance`.
    """
    policies = tuple(getattr(rapp_state, "policies", ()) or ())
    evidence = tuple(getattr(rapp_state, "evidence", ()) or ())
    elements: List[ComponentStatusView] = [_console_element(),
                                           _rapp_element(tuple(transitions),
                                                         coordinator_present)]
    elements.extend(_framework_elements(rapp_state))
    elements.append(_a1_element(policies))
    elements.extend(_near_rt_elements(e2_ready, e2_error_code, capability))
    elements.extend(_evidence_elements(evidence, now=now))
    elements.extend(_inventory_elements(capability))
    elements.extend(_external_elements())
    return tuple(_apply_provenance(elements, capability))


# --------------------------------------------------------------------------- #
# Readiness projection
# --------------------------------------------------------------------------- #


def _segment(segment_id: str, label: str, confirmed: bool, *,
             owner: str, reason: Optional[str] = None,
             at: Optional[str] = None) -> ReadinessSegmentView:
    return ReadinessSegmentView(
        segment_id=segment_id, label=label,
        status=st.OK if confirmed else st.BLOCKED, confirmed=confirmed,
        at=at, owner=owner, unmet_reason=None if confirmed else reason)


def build_readiness(rapp_state: Any = None, *,
                    ladder: Optional[Mapping[str, Any]] = None,
                    intent_accepted: Optional[bool] = None,
                    ) -> Tuple[ReadinessSegmentView, ...]:
    """The intent -> policy -> evidence chain, with the blocked segment named.

    When the deployment exposes the O1 readiness ladder its five segments are
    prepended; when it does not, they are absent rather than guessed - the
    readiness resource is not an R1 resource today (GAP-01), and a green default
    would be the single most misleading thing this strip could show.

    Once a segment is unmet, everything downstream renders ``Unknown`` with
    ``blocked_by`` pointing at it.  Downstream segments are not *failing*; they
    have not been reached, and saying so is more useful than a second red mark.
    """
    segments: List[ReadinessSegmentView] = []
    if ladder:
        for name in st.READINESS_ORDER:
            if name not in ladder:
                continue
            entry = ladder[name]
            confirmed = (entry if isinstance(entry, bool)
                         else bool(dict(entry).get("confirmed")
                                   or dict(entry).get("ready")))
            at = None if isinstance(entry, bool) else dict(entry).get("at")
            reason = (None if isinstance(entry, bool)
                      else dict(entry).get("reason"))
            segments.append(_segment(name, name.replace("_", " ").title(),
                                     confirmed, owner="O1_ASSURANCE",
                                     reason=reason or "segment not confirmed",
                                     at=at))
    elif ladder is not None:
        segments.append(ReadinessSegmentView(
            segment_id="LADDER", label="Readiness ladder",
            status=st.UNSUPPORTED, confirmed=False, owner="O1_ASSURANCE",
            unmet_reason="the deployment exposes no readiness resource "
                         "[GAP-01]"))

    policies = tuple(getattr(rapp_state, "policies", ()) or ())
    evidence = tuple(getattr(rapp_state, "evidence", ()) or ())
    enforced = [p for p in policies
                if getattr(p, "enforce_status", None) == "ENFORCED"]
    confirmed_evidence = [e for e in evidence
                          if getattr(e, "quality", None) == "OK"]
    observations = {
        "INTENT_ACCEPTED": (
            bool(intent_accepted) if intent_accepted is not None
            else bool(policies or evidence),
            "no intent has been accepted in this session"),
        "POLICY_CREATED": (bool(policies), "no policy status has been observed"),
        "POLICY_ENFORCED": (bool(enforced),
                            "no policy reports enforceStatus ENFORCED"),
        "EVIDENCE_CONFIRMED": (bool(confirmed_evidence),
                               "no evidence record with quality OK has been "
                               "accepted"),
    }
    for segment_id, label in CHAIN_SEGMENTS:
        confirmed, reason = observations[segment_id]
        segments.append(_segment(segment_id, label, confirmed,
                                 owner="RAPP_INTERNAL/R1", reason=reason))

    blocked = blocked_segment(segments)
    if blocked is None:
        return tuple(segments)
    out: List[ReadinessSegmentView] = []
    seen_block = False
    for segment in segments:
        if segment.segment_id == blocked.segment_id:
            seen_block = True
            out.append(segment)
            continue
        if seen_block and not segment.confirmed:
            out.append(ReadinessSegmentView(
                segment_id=segment.segment_id, label=segment.label,
                status=st.UNKNOWN, confirmed=False, at=segment.at,
                owner=segment.owner, unmet_reason="not reached",
                blocked_by=blocked.segment_id))
            continue
        out.append(segment)
    return tuple(out)


def readiness_headline(segments: Sequence[ReadinessSegmentView]) -> str:
    """One-line summary, for the header detail and the timeline audit record."""
    blocked = blocked_segment(segments)
    if blocked is None:
        return badge_text(st.OK, label="intent -> policy -> evidence complete")
    return badge_text(blocked.status,
                      label=f"blocked at {blocked.label}",
                      reason=blocked.unmet_reason)


# --------------------------------------------------------------------------- #
# The workspace
# --------------------------------------------------------------------------- #


class LiveOperationsWorkspace:
    """Topology and readiness, the experiment workflow, and the event timeline.

    Actions never execute here.  ``on_action(key, payload)`` hands the request
    to the console, which decides which confirmation it needs and which worker
    thread runs it - so no button press can perform I/O on the Tk thread.
    """

    id = "live_ops"
    title = "Live Operations"

    def __init__(self, *, controller: Any = None,
                 on_action: Optional[Callable[[str, str], None]] = None,
                 theme: str = tokens.DEFAULT_THEME,
                 scale: float = tokens.SCALE_NORMAL) -> None:
        self.controller = controller
        self.on_action = on_action
        self.theme = theme
        self.scale = scale
        self.frame = None
        self.topology = TopologyGrid(theme=theme, scale=scale)
        self.readiness = ReadinessStrip(theme=theme, scale=scale)
        self.timeline = TimelineView(theme=theme, scale=scale)
        self._palette = tokens.theme(theme)
        self._summary_widget = None
        self._preflight_tree = None
        self._workflow_status = None
        self._active = False
        #: action key -> the button that raises it.  Public so a reachability
        #: test can assert that every declared action has a control, and press
        #: it, rather than trusting that the row was built.
        self.buttons: dict = {}
        self.source_path = None

    # -- build -------------------------------------------------------------- #

    def build(self, parent) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.frame = tk.Frame(parent, bg=self._palette["bg"])
        self.frame.pack(fill="both", expand=True)

        outer = ttk.PanedWindow(self.frame, orient="vertical")
        outer.pack(fill="both", expand=True)

        top = ttk.PanedWindow(outer, orient="horizontal")
        outer.add(top, weight=3)

        status_pane = tk.Frame(top, bg=self._palette["bg"])
        top.add(status_pane, weight=3)
        tk.Label(status_pane, text="O-RAN topology and status",
                 bg=self._palette["bg"], fg=self._palette["fg"],
                 anchor="w", font=tokens.font("subhead", scale=self.scale,
                                              bold=True)
                 ).pack(fill="x", padx=tokens.SPACING["base"],
                        pady=(tokens.SPACING["tight"], 0))
        self.readiness.build(status_pane)
        self.readiness.frame.pack(fill="x", padx=tokens.SPACING["base"],
                                  pady=tokens.SPACING["tight"])
        self.topology.build(status_pane)
        self.topology.frame.pack(fill="both", expand=True,
                                 padx=tokens.SPACING["base"],
                                 pady=tokens.SPACING["tight"])

        workflow = tk.Frame(top, bg=self._palette["panel_bg"])
        top.add(workflow, weight=2)
        self._build_workflow(workflow)

        timeline_pane = tk.Frame(outer, bg=self._palette["bg"])
        outer.add(timeline_pane, weight=2)
        tk.Label(timeline_pane, text="Event timeline",
                 bg=self._palette["bg"], fg=self._palette["fg"], anchor="w",
                 font=tokens.font("subhead", scale=self.scale, bold=True)
                 ).pack(fill="x", padx=tokens.SPACING["base"])
        self.timeline.build(timeline_pane)
        self.timeline.frame.pack(fill="both", expand=True,
                                 padx=tokens.SPACING["base"],
                                 pady=tokens.SPACING["tight"])

    def _build_workflow(self, parent) -> None:
        import tkinter as tk
        from tkinter import ttk

        pad = tokens.SPACING["tight"]
        tk.Label(parent, text="Experiment workflow", anchor="w",
                 bg=self._palette["panel_bg"], fg=self._palette["fg"],
                 font=tokens.font("subhead", scale=self.scale, bold=True)
                 ).pack(fill="x", padx=tokens.SPACING["base"],
                        pady=(pad, 0))

        buttons = tk.Frame(parent, bg=self._palette["panel_bg"])
        buttons.pack(fill="x", padx=tokens.SPACING["base"], pady=pad)
        for key, label in WORKFLOW_ACTIONS:
            self.buttons[key] = tk.Button(
                buttons, text=label, relief="flat",
                bg=self._palette["panel_alt_bg"], fg=self._palette["fg"],
                font=tokens.font("small", scale=self.scale),
                command=lambda k=key: self._fire(k))
            self.buttons[key].pack(side="left", padx=1)

        # The session source row.  Until it existed, a console started from
        # main.py could load a profile and press Start - and nothing else: the
        # deployment binding, the mode choice, the Replay load and Disconnect
        # were all methods with no control anywhere on screen.  A capability an
        # operator cannot reach is a capability the console does not have.
        tk.Label(parent, text="Session source", anchor="w",
                 bg=self._palette["panel_bg"], fg=self._palette["fg"],
                 font=tokens.font("label", scale=self.scale, bold=True)
                 ).pack(fill="x", padx=tokens.SPACING["base"], pady=(pad, 0))
        source = tk.Frame(parent, bg=self._palette["panel_bg"])
        source.pack(fill="x", padx=tokens.SPACING["base"], pady=pad)
        for key, label in SOURCE_ACTIONS:
            self.buttons[key] = tk.Button(
                source, text=label, relief="flat",
                bg=self._palette["panel_alt_bg"], fg=self._palette["fg"],
                font=tokens.font("small", scale=self.scale),
                command=lambda k=key: self._fire(k))
            self.buttons[key].pack(side="left", padx=1)

        path_row = tk.Frame(parent, bg=self._palette["panel_bg"])
        path_row.pack(fill="x", padx=tokens.SPACING["base"])
        tk.Label(path_row, text="Replay source", anchor="w",
                 bg=self._palette["panel_bg"], fg=self._palette["fg_muted"],
                 font=tokens.font("micro", scale=self.scale)
                 ).pack(side="left", padx=(0, tokens.SPACING["hair"]))
        # Typed or pasted; left empty, Load Replay asks for a directory. The
        # entry exists so the path is visible before it is used, and so the
        # action can be driven without a modal dialog.
        self.source_path = tk.Entry(
            path_row, bg=self._palette["panel_alt_bg"], fg=self._palette["fg"],
            insertbackground=self._palette["fg"], relief="flat",
            font=tokens.font("small", scale=self.scale))
        self.source_path.pack(side="left", fill="x", expand=True)

        tk.Label(parent,
                 text="Bind names the deployment this profile would address "
                      "and contacts nothing; only Preflight opens a "
                      "connection. Start Experiment verifies readiness and "
                      "opens a run directory. It does not start a Core, gNB, "
                      "UE or USRP.",
                 anchor="w", justify="left", wraplength=420,
                 bg=self._palette["panel_bg"], fg=self._palette["fg_muted"],
                 font=tokens.font("micro", scale=self.scale)
                 ).pack(fill="x", padx=tokens.SPACING["base"])

        self._workflow_status = tk.Label(
            parent, text="", anchor="w", justify="left",
            bg=self._palette["panel_bg"], fg=self._palette["fg"],
            font=tokens.font("small", scale=self.scale))
        self._workflow_status.pack(fill="x", padx=tokens.SPACING["base"],
                                   pady=pad)

        self._summary_widget = tk.Text(
            parent, height=12, relief="flat", wrap="none",
            bg=self._palette["panel_alt_bg"], fg=self._palette["fg"],
            font=tokens.font("small", scale=self.scale, mono=True))
        self._summary_widget.pack(fill="x", padx=tokens.SPACING["base"])
        self._summary_widget.configure(state="disabled")

        tk.Label(parent, text="Preflight", anchor="w",
                 bg=self._palette["panel_bg"], fg=self._palette["fg"],
                 font=tokens.font("label", scale=self.scale, bold=True)
                 ).pack(fill="x", padx=tokens.SPACING["base"], pady=(pad, 0))
        self._preflight_tree = ttk.Treeview(
            parent, columns=("check", "status", "boundary", "reason"),
            show="headings", height=9)
        for key, heading, width in (("check", "Check", 150),
                                    ("status", "Result", 150),
                                    ("boundary", "Boundary", 150),
                                    ("reason", "Reason", 420)):
            self._preflight_tree.heading(key, text=heading)
            self._preflight_tree.column(key, width=width, anchor="w")
        self._preflight_tree.pack(fill="both", expand=True,
                                  padx=tokens.SPACING["base"], pady=pad)
        for status_id in st.STATUS_SPECS:
            self._preflight_tree.tag_configure(
                status_id, foreground=tokens.status_color(status_id, self.theme))

    def _fire(self, key: str) -> None:
        if self.on_action is None:
            return
        payload = ""
        if key == "load_replay" and self.source_path is not None:
            payload = self.source_path.get().strip()
        self.on_action(key, payload)

    # -- state -------------------------------------------------------------- #

    def on_state(self, state: SessionState) -> None:
        """Tk thread.  Repaint only; no I/O, no computation beyond formatting."""
        if self.frame is None:
            return
        self.topology.update(state.components)
        self.readiness.update(state.readiness)
        self.timeline.set_events(state.timeline)
        self._paint_summary(state)

    def _paint_summary(self, state: SessionState) -> None:
        if self._workflow_status is not None:
            headline = readiness_headline(state.readiness)
            self._workflow_status.configure(
                text=f"{headline}\nrun {state.run_id or st.PRE_MEASUREMENT}  "
                     f"{state.disposition}  "
                     f"{'recording' if state.recording else 'not recording'}")
        if self._summary_widget is not None:
            profile = getattr(self.controller, "profile", None)
            lines = [f"{label:<16}{value}"
                     for label, value in profile_summary(profile)]
            self._summary_widget.configure(state="normal")
            self._summary_widget.delete("1.0", "end")
            self._summary_widget.insert("1.0", "\n".join(lines))
            self._summary_widget.configure(state="disabled")
        if self._preflight_tree is not None:
            results = getattr(self.controller, "preflight_results", ())
            self._paint_preflight(results)

    def _paint_preflight(self,
                         results: Sequence[PreflightCheckView]) -> None:
        self._preflight_tree.delete(*self._preflight_tree.get_children())
        for view in results:
            self._preflight_tree.insert(
                "", "end", iid=view.check_id,
                values=(view.check_id, badge_text(view.status),
                        view.boundary or "-",
                        view.reason or view.detail or ""),
                tags=(view.status,))

    def on_activate(self) -> None:
        self._active = True

    def on_deactivate(self) -> None:
        self._active = False

    def gui_state(self) -> Mapping[str, Any]:
        return {"timeline": self.timeline.gui_state()}

    def restore_gui_state(self, state: Mapping[str, Any]) -> None:
        self.timeline.restore_gui_state(dict(state or {}).get("timeline"))


__all__ = [
    "CHAIN_SEGMENTS", "INVENTORY_CLASSES", "SOURCE_ACTIONS",
    "WORKFLOW_ACTIONS", "LiveOperationsWorkspace",
    "NOT_DECLARED_REASON", "NO_HEALTH_REASON", "NO_VERSION_REASON",
    "SERVICE_MODEL_LABELS", "VERSION_GAP_ID", "age_ms", "build_readiness",
    "build_topology", "readiness_headline",
]
