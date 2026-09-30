"""Which objective may be submitted, and what its registry record becomes after.

Two jobs, both deliberately refusing rather than deciding.

:func:`resolve_submittable_family` is the gate ``run_ota --objective`` passes
through.  It reads the Gate 4 registry and the family module and refuses by
name when either says this deployment cannot accept the objective.  It does not
consult the frozen policy schema to look for a way around the refusal: the
registry's ``blocking_reasons`` are a recorded judgement, and a runner that
could talk itself past them would make the record decorative.

:func:`plan_registry_transition` builds what a record *would* become once an OTA
run has produced evidence, and validates it against the registry's own rules --
without writing it anywhere.  Gate 5 stage 1 is preparation, so the transition
is prepared and left unapplied; the run has not happened yet, and a record that
claimed OTA evidence before there was any is the exact overstatement
``validate_record`` exists to catch.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Mapping, Sequence, Tuple

from assurance.objectives import (
    FAMILY_MODULES,
    EvidenceLevel,
    ObjectiveRecord,
    SupportState,
    record_for,
    validate_record,
)

__all__ = [
    "ObjectiveRefused",
    "plan_registry_transition",
    "resolve_submittable_family",
    "submission_readiness",
]


class ObjectiveRefused(RuntimeError):
    """This deployment does not accept a submission for the named objective."""


def submission_readiness(family: str) -> Mapping[str, Any]:
    """What the registry and the family module say about submitting *family*."""
    record = record_for(family)
    module = FAMILY_MODULES[family]()
    lifecycle = module.policy_lifecycle()
    return {
        "family": family,
        "supportState": record.support_state.value,
        "evidenceLevel": record.evidence_level.value,
        "registrySubmittable": record.deployment_capability.submittable,
        "registryPolicyTypeId": record.standard_mapping.policy_type_id,
        "familyModulePolicyTypeId": lifecycle.policy_type_id,
        "blockingReasons": list(record.deployment_capability.blocking_reasons),
        "evidenceRefs": list(record.evidence_refs),
    }


def resolve_submittable_family(family: str) -> Any:
    """Return the family module, or refuse with the recorded reasons.

    Both halves must agree.  The family module knowing a policy type is not
    enough if the registry recorded why the deployment will not take it, and a
    registry that said yes would still be refused by a module with no policy
    type -- the two are independent statements and this run needs both.
    """
    if family not in FAMILY_MODULES:
        raise ObjectiveRefused(
            f"{family!r} is not an objective family; known: "
            + ", ".join(sorted(FAMILY_MODULES))
        )
    readiness = submission_readiness(family)
    if readiness["familyModulePolicyTypeId"] is None:
        raise ObjectiveRefused(
            f"{family} declares no policy type on this deployment; there is "
            "nothing to submit"
        )
    if not readiness["registrySubmittable"]:
        reasons = "; ".join(readiness["blockingReasons"]) or "no reason recorded"
        raise ObjectiveRefused(
            f"the registry records {family} as not submittable on the current "
            f"frozen deployment: {reasons}"
        )
    return FAMILY_MODULES[family]()


def plan_registry_transition(
    family: str,
    *,
    to_state: SupportState,
    evidence_refs: Sequence[str] = (),
    scenarios: Sequence[str] = (),
) -> Tuple[ObjectiveRecord, Mapping[str, Any]]:
    """Build and validate the record this family *would* carry, without saving it.

    Returns the candidate record and a diff of what changed, so a reviewer can
    see the claim before anything claims it.  ``validate_record`` is run on the
    candidate, which is what stops a transition being planned that the registry
    would refuse -- notably ``OTA_LIVE_VERIFIED`` without evidence references,
    or for an objective whose deployment capability still says no.
    """
    current = record_for(family)
    evidence_level = current.evidence_level
    if to_state is SupportState.OTA_LIVE_VERIFIED:
        evidence_level = EvidenceLevel.OTA_RAW_EVIDENCE
    candidate = dataclasses.replace(
        current,
        support_state=to_state,
        evidence_level=evidence_level,
        evidence_refs=tuple(evidence_refs) or current.evidence_refs,
        hardware_free_scenarios=tuple(scenarios) or current.hardware_free_scenarios,
    )
    validate_record(candidate)
    diff = {
        "family": family,
        "supportState": [current.support_state.value, candidate.support_state.value],
        "evidenceLevel": [current.evidence_level.value, candidate.evidence_level.value],
        "evidenceRefs": [list(current.evidence_refs), list(candidate.evidence_refs)],
    }
    return candidate, diff
