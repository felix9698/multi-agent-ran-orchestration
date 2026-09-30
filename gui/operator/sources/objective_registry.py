"""Project the objective registry onto the operator's screen.  Read-only.

Owner lane: **OBJ3** (``docs/architecture/SEAMS-GATE4.md`` section 3).  Gate 4's
fourth acceptance item is exactly this: "objective별 actual capability/evidence
level이 registry와 GUI에 정직하게 표시" (task section 13).

Pure python, no toolkit import, like every module under ``gui/operator/sources``
-- and, unlike the others, no worker either: the registry is a literal, so this
is a projection of data that is already decided.  That direction matters more
here than anywhere else in the console:

**The GUI is not a second judge.**  Support state, evidence level, submittability
and every blocking reason are read verbatim out of
:func:`assurance.objectives.registry.registry_view`.  Nothing here computes a
verdict, upgrades a state, or hides a reason; the functions below choose a
*glyph and a colour* for a value the registry already fixed, and choosing wrong
can only make the screen more pessimistic than the record, never more generous.
The import direction is one-way for the same reason (Gate 2 boundary):
``gui`` reads ``assurance``, and ``assurance`` never imports ``gui``.

**A hardware-free result must not read as an OTA success.**  Gate 4's third
acceptance item.  ``ObjectiveRecord.as_dict()["evidenceIsOta"]`` already carries
that judgement -- it is ``True`` only for
:attr:`~assurance.objectives.registry.EvidenceLevel.OTA_RAW_EVIDENCE` -- so this
module draws that field rather than deciding again.  :data:`EVIDENCE_STATUS`
gives the ``OK`` status to that level and to no other, and
:data:`EVIDENCE_LABELS` says "not OTA evidence" in words next to the ones that
are not, because a colour alone is not a statement and a screenshot in
greyscale must still say it.

**Two axes stay two axes.**  Support state and evidence level are rendered as
separate columns with separate glyphs, because they answer different questions:
how far this family's implementation and verification has got, and what has
actually been observed.  A family can be honestly ``HARDWARE_FREE_VERIFIED``
with no OTA evidence at all, and an operator who reads one column as the other
has been told something false by the layout.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from assurance.objectives.registry import registry_view

from .. import status as st

__all__ = [
    "EVIDENCE_LABELS",
    "EVIDENCE_STATUS",
    "ObjectiveRegistryView",
    "ObjectiveRow",
    "SUPPORT_LABELS",
    "SUPPORT_STATUS",
    "TABLE_COLUMNS",
    "detail_lines",
    "pane_lines",
    "project_registry",
    "summary_rows",
    "summary_text",
]


#: Support state -> GUI status id.  ``OTA_LIVE_VERIFIED`` is the only state that
#: resolves to ``OK``, and the validator is what makes that safe: a record
#: cannot enter it without retained OTA raw evidence and a submittable
#: deployment (``validate_record`` rule 6).  Everything else is drawn as
#: something other than success, which is the conservative direction.
SUPPORT_STATUS: Dict[str, str] = {
    "OTA_LIVE_VERIFIED": st.OK,
    "OTA_VERIFICATION_PENDING": st.DEGRADED,
    "HARDWARE_FREE_VERIFIED": st.DEGRADED,
    "IMPLEMENTATION_IN_PROGRESS": st.UNKNOWN,
    "UNSUPPORTED_BY_CURRENT_DEPLOYMENT": st.UNSUPPORTED,
}

#: What each support state says in words.  The state name is always shown as
#: well: these expand it, they do not replace it, and an operator reading a
#: screenshot must be able to tell a mock round trip from a radio run without
#: knowing the vocabulary.
SUPPORT_LABELS: Dict[str, str] = {
    "OTA_LIVE_VERIFIED": "verified on the testbed, raw evidence retained",
    "OTA_VERIFICATION_PENDING": "hardware-free verified; no OTA run yet",
    "HARDWARE_FREE_VERIFIED": "hardware-free matrix passed; NOT an OTA result",
    "IMPLEMENTATION_IN_PROGRESS": "contracts, catalog or matrix incomplete",
    "UNSUPPORTED_BY_CURRENT_DEPLOYMENT":
        "fails closed: a premise this objective needs is absent here",
}

#: Evidence level -> GUI status id.  Only OTA raw evidence is ``OK``.  A
#: complete hardware-free round trip is real work and a real result, and it is
#: still not evidence from a radio (design section 15), so it is drawn
#: ``DEGRADED`` rather than green.
EVIDENCE_STATUS: Dict[str, str] = {
    "OTA_RAW_EVIDENCE": st.OK,
    "HARDWARE_FREE_ROUND_TRIP": st.DEGRADED,
    "CONTRACT_DECLARED": st.UNAVAILABLE,
    "NONE": st.UNAVAILABLE,
}

EVIDENCE_LABELS: Dict[str, str] = {
    "OTA_RAW_EVIDENCE": "OTA raw evidence, retained",
    "HARDWARE_FREE_ROUND_TRIP": "hardware-free round trip - not OTA evidence",
    "CONTRACT_DECLARED": "contract and mapping declared - nothing observed",
    "NONE": "nothing observed",
}

#: The summary table, column by column: ``(heading, width)``.  Declared as data
#: so the headless projection and the rendered pane cannot drift into two
#: different tables.
TABLE_COLUMNS: Tuple[Tuple[str, int], ...] = (
    ("Objective family", 38),
    ("Support state", 36),
    ("Evidence level", 32),
    ("Submit", 22),
)


def _resolve(table: Mapping[str, str], value: Optional[str]) -> st.Resolved:
    """Resolve a registry enum value through one of the tables above.

    An unmapped value resolves to ``UNKNOWN`` with
    :data:`~gui.operator.status.UNMAPPED_REASON`, never to ``OK``: a state this
    console has not been taught is a state it must not paint as success.
    """
    if value is None:
        return st.resolve(st.UNKNOWN, reason="value absent")
    mapped = table.get(value)
    if mapped is None:
        return st.resolve(st.UNKNOWN, reason=f"{st.UNMAPPED_REASON}: {value!r}")
    return st.resolve(mapped)


@dataclass(frozen=True)
class ObjectiveRow:
    """One registry record, ready to draw.  Every field is read, none derived.

    Frozen, and built only by :func:`project_registry`, so a widget cannot hold
    one and change it: a projection the render layer could mutate would be a
    path from the screen back into what the screen claims.
    """

    family: str
    project_contract_id: str
    lane: str
    intent_summary: str
    support_state: str
    support: st.Resolved
    support_label: str
    evidence_level: str
    evidence: st.Resolved
    evidence_label: str
    #: The registry's own answer to "is this OTA evidence", not a second one.
    evidence_is_ota: bool
    submittable: bool
    submit: st.Resolved
    #: Verbatim and complete.  Never summarised to "unavailable": an operator
    #: told only that something is refused cannot tell a missing policy type
    #: from a missing measurement, and those have different answers.
    blocking_reasons: Tuple[str, ...]
    capability_basis: str
    policy_interface: str
    policy_type_id: Optional[str]
    policy_type_kind: str
    control_service_models: Tuple[str, ...]
    measurement_service_models: Tuple[str, ...]
    #: ``(rendering, delivered)`` per O1 measurement.  Delivery is shown because
    #: a mapped-but-undelivered measurement is the difference between a plan and
    #: a capability.
    o1_measurements: Tuple[Tuple[str, bool], ...]
    mapping_notes: str
    #: ``(kind, statement, basis)`` for premises the deployment does not have.
    unmet_premises: Tuple[Tuple[str, str, str], ...]
    met_premises: Tuple[Tuple[str, str], ...]
    component_families: Tuple[str, ...]
    joint_trial_required: bool
    hardware_free_scenarios: Tuple[str, ...]
    evidence_refs: Tuple[str, ...]
    related_contracts: Tuple[Tuple[str, str], ...]
    is_regression_contract: bool

    @property
    def reads_as_success(self) -> bool:
        """Whether this row is drawn as a completed, successful objective.

        The predicate Gate 4's third acceptance item is asserted against: a
        record whose evidence is not OTA must never make this ``True``.
        """
        return self.support.is_ok and self.evidence.is_ok


@dataclass(frozen=True)
class ObjectiveRegistryView:
    """The whole registry as the console shows it."""

    #: Task section 7.7, carried verbatim from the registry and shown on the
    #: pane: these names are project contract identifiers, not O-RAN ones.
    identifier_notice: str
    families: Tuple[str, ...]
    regression_contract: str
    rows: Tuple[ObjectiveRow, ...]

    def row(self, family: str) -> ObjectiveRow:
        for row in self.rows:
            if row.family == family:
                return row
        raise KeyError(family)


def _service_model_text(item: Mapping[str, Any]) -> str:
    action = item.get("actionId")
    action_text = (
        f" / Action {action} {item.get('actionName') or ''}".rstrip()
        if action is not None
        else ""
    )
    parameters = ", ".join(item.get("parameters") or ()) or "-"
    return (
        f"{item.get('serviceModel')} {item.get('version')} "
        f"RAN function {item.get('ranFunctionId')} {item.get('procedure')} "
        f"Style {item.get('styleId')} {item.get('styleName')}{action_text} "
        f"[{parameters}]"
    )


def _o1_text(item: Mapping[str, Any]) -> Tuple[str, bool]:
    delivered = bool(item.get("deliveredByDeployment"))
    text = (
        f"{item.get('name')} ({item.get('unit')}, {item.get('scopeLevel')}) "
        f"{item.get('definition')} {item.get('clause')}, "
        f"file format {item.get('fileFormat')}"
    )
    return text, delivered


def _row(record: Mapping[str, Any]) -> ObjectiveRow:
    capability = record.get("deploymentCapability") or {}
    mapping = record.get("standardMapping") or {}
    premises = record.get("premises") or []
    support_state = str(record.get("supportState"))
    evidence_level = str(record.get("evidenceLevel"))
    submittable = bool(capability.get("submittable"))
    return ObjectiveRow(
        family=str(record.get("family")),
        project_contract_id=str(record.get("projectContractId")),
        lane=str(record.get("lane")),
        intent_summary=str(record.get("intentSummary") or ""),
        support_state=support_state,
        support=_resolve(SUPPORT_STATUS, support_state),
        support_label=SUPPORT_LABELS.get(support_state, st.UNMAPPED_REASON),
        evidence_level=evidence_level,
        evidence=_resolve(EVIDENCE_STATUS, evidence_level),
        evidence_label=EVIDENCE_LABELS.get(evidence_level, st.UNMAPPED_REASON),
        evidence_is_ota=bool(record.get("evidenceIsOta")),
        submittable=submittable,
        submit=st.resolve(st.OK if submittable else st.BLOCKED),
        blocking_reasons=tuple(str(item) for item in
                               capability.get("blockingReasons") or ()),
        capability_basis=str(capability.get("basis") or ""),
        policy_interface=str(mapping.get("policyInterface") or ""),
        policy_type_id=(str(mapping["policyTypeId"])
                        if mapping.get("policyTypeId") else None),
        policy_type_kind=str(mapping.get("policyTypeKind") or ""),
        control_service_models=tuple(
            _service_model_text(item)
            for item in mapping.get("controlServiceModels") or ()),
        measurement_service_models=tuple(
            _service_model_text(item)
            for item in mapping.get("measurementServiceModels") or ()),
        o1_measurements=tuple(
            _o1_text(item) for item in mapping.get("o1Measurements") or ()),
        mapping_notes=str(mapping.get("notes") or ""),
        unmet_premises=tuple(
            (str(item.get("kind")), str(item.get("statement")),
             str(item.get("basis")))
            for item in premises if not item.get("met")),
        met_premises=tuple(
            (str(item.get("kind")), str(item.get("statement")))
            for item in premises if item.get("met")),
        component_families=tuple(str(item) for item in
                                 record.get("componentFamilies") or ()),
        joint_trial_required=bool(record.get("jointTrialRequired")),
        hardware_free_scenarios=tuple(
            str(item) for item in record.get("hardwareFreeScenarios") or ()),
        evidence_refs=tuple(str(item) for item in
                            record.get("evidenceRefs") or ()),
        related_contracts=tuple(
            (str(key), str(value))
            for key, value in sorted(
                (record.get("relatedContracts") or {}).items())),
        is_regression_contract=bool(record.get("isRegressionContract")),
    )


def project_registry(view: Optional[Mapping[str, Any]] = None) -> ObjectiveRegistryView:
    """Project the registry for the console.

    *view* defaults to :func:`assurance.objectives.registry.registry_view`; a
    caller may pass one in, which is how the tests drive the five support states
    through this projection without inventing a sixth path into the renderer.
    """
    source = registry_view() if view is None else view
    return ObjectiveRegistryView(
        identifier_notice=str(source.get("identifierNotice") or ""),
        families=tuple(str(item) for item in source.get("families") or ()),
        regression_contract=str(source.get("regressionContract") or ""),
        rows=tuple(_row(record) for record in source.get("records") or ()),
    )


def summary_rows(view: ObjectiveRegistryView) -> Tuple[Tuple[str, ...], ...]:
    """The summary table as text cells, header row first.

    Four columns, and the two middle ones are the two axes.  Each carries its
    own glyph so the distinction survives greyscale and projection -- the
    "never colour alone" rule of ``gui/operator/status.py``.
    """
    rows: List[Tuple[str, ...]] = [tuple(name for name, _width in TABLE_COLUMNS)]
    for row in view.rows:
        rows.append((
            row.family + (" [regression]" if row.is_regression_contract else ""),
            f"{row.support.glyph} {row.support_state}",
            f"{row.evidence.glyph} {row.evidence_level}",
            f"{row.submit.glyph} "
            + ("submittable" if row.submittable else "not submittable"),
        ))
    return tuple(rows)


def summary_text(view: ObjectiveRegistryView) -> Tuple[str, ...]:
    """The summary table rendered to fixed-width lines."""
    widths = [width for _name, width in TABLE_COLUMNS]
    lines = []
    for index, cells in enumerate(summary_rows(view)):
        lines.append("  ".join(
            str(cell).ljust(width) for cell, width in zip(cells, widths)).rstrip())
        if index == 0:
            lines.append("  ".join("-" * width for width in widths))
    return tuple(lines)


def detail_lines(row: ObjectiveRow) -> Tuple[str, ...]:
    """Everything the registry holds about one objective, as text.

    Long on purpose.  The acceptance item is that an operator can read the
    screen and know what is true of each objective, and the failure mode it
    names is a console that shows "unavailable" and stops -- so the blocking
    reasons appear in full, each unmet premise carries the basis it was read
    from, and a mapped-but-undelivered measurement says which it is.
    """
    lines: List[str] = [
        f"{row.support.glyph} {row.family}  [{row.project_contract_id}]"
        + ("  (preserved regression contract)"
           if row.is_regression_contract else ""),
        f"    intent            {row.intent_summary}",
        f"    owning lane       {row.lane}",
        "",
        f"    support state     {row.support.glyph} {row.support_state}"
        f"  -- {row.support_label}",
        f"    evidence level    {row.evidence.glyph} {row.evidence_level}"
        f"  -- {row.evidence_label}",
        "    (two axes: how far verification has got, and what was observed. "
        "A hardware-free",
        "     round trip is never OTA evidence, however complete.)",
        f"    OTA evidence      {'yes' if row.evidence_is_ota else 'no'}",
        f"    submittable now   {row.submit.glyph} "
        f"{'yes' if row.submittable else 'no'}",
    ]
    for reason in row.blocking_reasons:
        lines.append(f"      blocked because {reason}")
    if row.capability_basis:
        lines.append(f"      capability basis {row.capability_basis}")
    lines += [
        "",
        "    standard mapping (the published versions this project identifier "
        "is mapped onto)",
        f"      A1 interface    {row.policy_interface}",
        f"      policy type     {row.policy_type_id or st.PRE_MEASUREMENT}"
        f"  ({row.policy_type_kind})",
    ]
    for text in row.control_service_models:
        lines.append(f"      E2 control      {text}")
    for text in row.measurement_service_models:
        lines.append(f"      E2 report       {text}")
    for text, delivered in row.o1_measurements:
        mark = ("delivered by this deployment" if delivered
                else f"{st.resolve(st.UNAVAILABLE).glyph} NOT delivered "
                     f"by this deployment")
        lines.append(f"      O1 measurement  {text} -- {mark}")
    if row.mapping_notes:
        lines.append(f"      note            {row.mapping_notes}")
    if row.unmet_premises:
        lines.append("")
        lines.append("    premises this deployment does not have")
        for kind, statement, basis in row.unmet_premises:
            lines.append(f"      {st.resolve(st.BLOCKED).glyph} {kind}: "
                         f"{statement}")
            lines.append(f"          basis   {basis}")
    if row.met_premises:
        lines.append("")
        lines.append("    premises this deployment has")
        for kind, statement in row.met_premises:
            lines.append(f"      {st.resolve(st.OK).glyph} {kind}: {statement}")
    if row.component_families:
        lines += [
            "",
            f"    components        {', '.join(row.component_families)}",
            "    joint trial       "
            + ("every component must be judged inside ONE trial; separate "
               "passes may not be composed"
               if row.joint_trial_required else st.PRE_MEASUREMENT),
        ]
    if row.hardware_free_scenarios:
        lines.append("")
        lines.append("    hardware-free scenarios passed  "
                     + ", ".join(row.hardware_free_scenarios))
        lines.append("      (a hardware-free pass is not an OTA result)")
    if row.evidence_refs:
        lines.append("")
        lines.append("    retained OTA evidence")
        for reference in row.evidence_refs:
            lines.append(f"      {reference}")
    for name, text in row.related_contracts:
        lines.append("")
        lines.append(f"    related contract  {name}: {text}")
    return tuple(lines)


def pane_lines(view: Optional[ObjectiveRegistryView] = None) -> Tuple[str, ...]:
    """The complete pane: notice, summary table, then every record in full.

    One function so the rendered pane and any headless reading of it are the
    same text.  The renderer writes these lines into a disabled text widget and
    adds nothing of its own.
    """
    projection = project_registry() if view is None else view
    lines: List[str] = [
        "Objective registry - what is true of each objective today",
        "",
        f"  {projection.identifier_notice}",
        "",
        f"  seven families: {', '.join(projection.families)}",
        f"  preserved regression contract: {projection.regression_contract}",
        "",
    ]
    lines.extend(summary_text(projection))
    lines.append("")
    for row in projection.rows:
        lines.extend(detail_lines(row))
        lines.append("")
    return tuple(lines)
