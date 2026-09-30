"""Deterministic (LLM-free) implementations of the Intent and xApp roles.

Owner lane: **KAGT**.  New file, added under
``docs/architecture/SEAMS-GATE2.md`` section 3.

Both classes are constructed with nothing but an id string: no Kernel, no
Write Gateway, no R1 client, no ledger, matching ``roles.py``'s "constructed
with read-only views and a submit callback" -- there is no submit callback
either, because sealing and sending is
:mod:`assurance.advisors.mailbox`'s job, not the agent's.  Everything these
classes see arrives as a method argument and is read, never mutated in place.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence, Tuple

from assurance.advisors.grammar import IntentGrammarEntry, parse_utterance
from assurance.advisors.messages import (
    MAX_EXPLANATION_CHARS,
    AdvisoryKind,
    AdvisoryMessage,
    CandidateAssessment,
    IntentDraft,
)
from assurance.contracts.capability import CapabilityManifest
from assurance.contracts.target import TypedConstraint
from assurance.core.components import ComponentId
from assurance.core.provenance import DocumentStatus, Provenance, TypedQuantity

__all__ = ["DeterministicIntentAgent", "RuleBasedXAppAgent"]


def _capped(text: str) -> str:
    return text[:MAX_EXPLANATION_CHARS]


class DeterministicIntentAgent:
    """Grammar-based :class:`~assurance.advisors.roles.IntentAgent`.

    ``capability_view`` is accepted, per the frozen signature, but the Gate 2
    grammar selects an objective from keywords alone; a capability-aware
    refinement (filtering ``objective_registry`` by what is actually
    deployed before matching) is a natural extension and not this baseline's
    job.
    """

    def __init__(self, *, agent_id: str = "intent-agent-deterministic") -> None:
        self._agent_id = agent_id
        self._sequence = 0

    def draft_contract(
        self,
        *,
        utterance: str,
        objective_registry: Mapping[str, IntentGrammarEntry],
        capability_view: Sequence[Any],
        correlation_id: str,
        epoch_hash: str,
        now: str,
    ) -> AdvisoryMessage:
        objective_family, scope_selector, constraints, unsupported = parse_utterance(
            utterance, objective_registry=objective_registry, source_record=self._agent_id,
        )
        self._sequence += 1
        if constraints:
            summary = f"read as objective {objective_family!r} with {len(constraints)} draft constraint(s)."
        else:
            summary = f"read as objective {objective_family!r}; no numeric bound could be drafted."
        return AdvisoryMessage(
            message_id=f"{self._agent_id}/{correlation_id}/{self._sequence}",
            kind=AdvisoryKind.INTENT_DRAFT,
            issued_by=ComponentId.INTENT_AGENT,
            correlation_id=correlation_id,
            epoch_hash=epoch_hash,
            created_at=now,
            body=IntentDraft(
                objective_family=objective_family,
                scope_selector=scope_selector,
                proposed_constraints=constraints,
                unsupported_requests=unsupported,
                explanation=_capped(f"'{utterance}' -> {summary}"),
            ),
        )

    def explain(self, *, subject_ref: str, state_view: Mapping[str, Any]) -> str:
        if not state_view:
            return _capped(f"{subject_ref}: no additional state")
        parts = ", ".join(f"{key}={value!r}" for key, value in sorted(state_view.items()))
        return _capped(f"{subject_ref}: {parts}")


def _find_manifest(
    capability_ref: str, capability_view: Sequence[Any]
) -> Optional[CapabilityManifest]:
    for manifest in capability_view:
        candidate_id = getattr(manifest, "capability_id", None)
        contract_id = getattr(manifest, "contract_id", None)
        if capability_ref in (candidate_id, contract_id):
            return manifest
    return None


def _draft_copy(constraint: TypedConstraint, *, source_record: str) -> TypedConstraint:
    """Re-issue a manifest constraint's bound as a DRAFT expectation.

    An advisory may carry only DRAFT quantities (design section 4.2); a
    capability manifest's constraints are frozen, admissible contract
    content, so the assessment re-stamps a copy rather than forwarding the
    admissible original -- an expected effect, never itself a number a Kernel
    decision could be made from.
    """
    original = constraint.bound
    if original.provenance is Provenance.DERIVED:
        draft_bound = TypedQuantity(
            original.value, original.unit, Provenance.DERIVED, source_record,
            DocumentStatus.DRAFT, derivation_rule=original.derivation_rule,
            input_refs=original.input_refs,
        )
    else:
        draft_bound = TypedQuantity(
            original.value, original.unit, original.provenance, source_record,
            DocumentStatus.DRAFT,
        )
    return TypedConstraint(
        measurement_ref=constraint.measurement_ref,
        operator=constraint.operator,
        bound=draft_bound,
        allowed_values=constraint.allowed_values,
    )


class RuleBasedXAppAgent:
    """Deterministic capability-manifest matcher for the xApp Agent role.

    Applicability is structural: a candidate is applicable only when its
    ``capability_ref`` names a capability manifest actually present in
    ``capability_view``.  An xApp cannot honestly rate a capability it cannot
    see, and "an agent that rates everything applicable produces a catalog
    sweep with extra steps" (``roles.py``) -- so absence is a firm ``False``,
    never a guess.
    """

    def __init__(self, *, agent_id: str = "xapp-agent-rule-based") -> None:
        self._agent_id = agent_id
        self._sequence = 0

    def assess_candidate(
        self,
        *,
        candidate: Any,
        capability_view: Sequence[Any],
        measurement_view: Sequence[Any],
        correlation_id: str,
        epoch_hash: str,
        now: str,
    ) -> AdvisoryMessage:
        manifest = _find_manifest(candidate.capability_ref, capability_view)
        applicable = manifest is not None
        expected_effect: Tuple[TypedConstraint, ...] = ()
        evidence_needs: Tuple[str, ...] = ()
        if manifest is not None:
            expected_effect = tuple(
                _draft_copy(constraint, source_record=candidate.candidate_id)
                for constraint in manifest.constraints
            )
            evidence_needs = tuple(manifest.measurement_refs)
            risk_notes = (
                f"capability {candidate.capability_ref!r} registered; "
                f"assessed from {len(manifest.constraints)} manifest constraint(s)."
            )
        else:
            risk_notes = (
                f"capability {candidate.capability_ref!r} not found in capability_view; "
                "cannot be assessed as applicable."
            )

        self._sequence += 1
        return AdvisoryMessage(
            message_id=f"{self._agent_id}/{correlation_id}/{self._sequence}",
            kind=AdvisoryKind.CANDIDATE_ASSESSMENT,
            issued_by=ComponentId.XAPP_AGENT,
            correlation_id=correlation_id,
            epoch_hash=epoch_hash,
            created_at=now,
            body=CandidateAssessment(
                candidate_id=candidate.candidate_id,
                applicable=applicable,
                expected_effect=expected_effect,
                evidence_needs=evidence_needs,
                risk_notes=_capped(risk_notes),
            ),
        )
