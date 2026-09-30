"""Typed advisory messages.

Complete module: pure data with constructor-enforced invariants, no owner.

Design section 4.2: the Intent Agent, the xApp Agent(s) and the Evidence
Coordinator "are advisory.  They cannot add candidates, change targets, issue
actuator commands, assign verdicts, modify ledgers, release a target vector, or
terminate a case.  Their messages pass through a typed Kernel mailbox."

This module is that message type, and it is typed all the way down: there is no
free-form ``payload`` dict an agent can put anything into.  Three concrete
bodies, one per role, each with a fixed field list -- so "the agent proposed
something the schema has no room for" is a construction failure rather than a
field the Kernel has to remember to ignore.

Two invariants are enforced in the constructor because they are the difference
between an advisory and an instruction:

**Proposed numbers are inadmissible by construction.**  Every
:class:`~assurance.core.provenance.TypedQuantity` inside an advisory must carry
:attr:`~assurance.core.provenance.DocumentStatus.DRAFT`, which makes
:attr:`~assurance.core.provenance.TypedQuantity.admissible_for_runtime` false.
An agent-proposed threshold therefore *cannot* be used for admission, a harm
bound or a success threshold until the Operator confirms it and the Kernel
re-stamps it as ``OPERATOR_CONFIRMED``.  This is the direct cutover for GAP-02
in ``docs/architecture/GATE1-MAP.md``, where a model-produced confidence float
was compared against a threshold and changed both admission and ledger state.

**Free text is capped and inert.**  Explanations exist for the Operator
(design section 4.2: "produces Operator-facing explanations") and are display
data.  They are length-capped, never parsed, and never an input to a verdict,
a charge, a closure or a candidate choice.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, FrozenSet, Final, Mapping, Optional, Tuple, Union

from assurance.contracts.target import TypedConstraint
from assurance.core.addressing import content_hash, is_content_hash
from assurance.core.components import ADVISORY_COMPONENTS, ComponentId
from assurance.core.provenance import DocumentStatus, TypedQuantity
from assurance.core.timebase import is_utc_timestamp

__all__ = [
    "AdvisoryBody",
    "AdvisoryKind",
    "AdvisoryMessage",
    "CandidateAssessment",
    "FORBIDDEN_ADVISORY_FIELDS",
    "IntentDraft",
    "MAX_EXPLANATION_CHARS",
    "NextCandidateProposal",
]

#: Hard cap on any Operator-facing free text in an advisory.  An unbounded
#: explanation is both a denial-of-service surface on the Cockpit and an
#: invitation to smuggle structure into prose.
MAX_EXPLANATION_CHARS: Final[int] = 2000

#: Field names no advisory body may ever carry.  Each names a thing design
#: section 4.2 reserves to the Kernel; the seam test asserts the intersection
#: with every body's real fields is empty.
FORBIDDEN_ADVISORY_FIELDS: FrozenSet[str] = frozenset(
    {
        "actuator_command",
        "authorization",
        "case_termination",
        "catalog_membership",
        "charge",
        "confidence",
        "evidence_closure",
        "fencing_token",
        "harm_charge",
        "ledger_update",
        "policy_body",
        "target_release",
        "terminal_state",
        "theta_star",
        "threshold_override",
        "token",
        "verdict",
    }
)


class AdvisoryKind(Enum):
    """One kind per advisory role (design section 4.2)."""

    #: Intent Agent: natural language turned into a typed contract draft.
    INTENT_DRAFT = "INTENT_DRAFT"
    #: xApp Agent: applicability, expected effect, risk, evidence needs for a
    #: registered capability.
    CANDIDATE_ASSESSMENT = "CANDIDATE_ASSESSMENT"
    #: Evidence Coordinator: which catalog candidate to try next, and which
    #: evidence obligations that would serve.
    NEXT_CANDIDATE_PROPOSAL = "NEXT_CANDIDATE_PROPOSAL"


def _check_draft_quantities(values: Any, where: str) -> None:
    """Refuse any non-``DRAFT`` quantity reachable from an advisory body."""
    if isinstance(values, TypedQuantity):
        if values.document_status is not DocumentStatus.DRAFT:
            raise ValueError(
                f"{where}: an advisory carries DRAFT quantities only; "
                f"{values.document_status.value} would be admissible at runtime, "
                "and an agent cannot set an admissible number (design section 4.2)"
            )
        return
    if isinstance(values, TypedConstraint):
        _check_draft_quantities(values.bound, where)
        return
    if isinstance(values, Mapping):
        for item in values.values():
            _check_draft_quantities(item, where)
        return
    if isinstance(values, (list, tuple)):
        for item in values:
            _check_draft_quantities(item, where)


def _check_explanation(text: str, field_name: str) -> None:
    if not isinstance(text, str):
        raise TypeError(f"{field_name} must be a string")
    if len(text) > MAX_EXPLANATION_CHARS:
        raise ValueError(
            f"{field_name} exceeds {MAX_EXPLANATION_CHARS} characters; "
            "Operator-facing explanation is capped display data"
        )


@dataclass(frozen=True)
class IntentDraft:
    """The Intent Agent's typed reading of a natural-language intent.

    Attributes
    ----------
    objective_family:
        Which project objective the agent believes the Operator asked for.
        A *name from the frozen registry* -- the agent selects, it does not
        invent an objective.
    scope_selector:
        The entities the agent read out of the request.
    proposed_constraints:
        Draft predicates.  Every bound is a ``DRAFT``
        :class:`~assurance.core.provenance.TypedQuantity`, so none of them can
        be used as a success threshold until the Operator confirms.
    unsupported_requests:
        Parts of the intent this deployment cannot serve.  Present so the
        agent has somewhere honest to put them; design section 10 requires
        unsupported fields to fail closed rather than be quietly dropped.
    explanation:
        Operator-facing text.  Display only.
    """

    objective_family: str
    scope_selector: Mapping[str, str]
    proposed_constraints: Tuple[TypedConstraint, ...] = ()
    unsupported_requests: Tuple[str, ...] = ()
    explanation: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "scope_selector",
            {str(k): str(v) for k, v in dict(self.scope_selector).items()},
        )
        object.__setattr__(self, "proposed_constraints", tuple(self.proposed_constraints))
        object.__setattr__(self, "unsupported_requests", tuple(self.unsupported_requests))
        _check_draft_quantities(self.proposed_constraints, "IntentDraft.proposed_constraints")
        _check_explanation(self.explanation, "IntentDraft.explanation")


@dataclass(frozen=True)
class CandidateAssessment:
    """An xApp Agent's assessment of one frozen catalog candidate.

    ``candidate_id`` must already exist in the frozen catalog; the Kernel
    refuses an assessment of anything else, because accepting one would be the
    first step toward an agent introducing a candidate (design section 6.3).
    """

    candidate_id: str
    applicable: bool
    #: Draft expectation of the effect, for planning and for the Cockpit.
    #: Never compared against a measurement to decide anything.
    expected_effect: Tuple[TypedConstraint, ...] = ()
    #: Evidence cell ids this candidate could contribute to.
    evidence_needs: Tuple[str, ...] = ()
    #: Risks the agent wants the Operator to see.  Display only.
    risk_notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "expected_effect", tuple(self.expected_effect))
        object.__setattr__(self, "evidence_needs", tuple(self.evidence_needs))
        _check_draft_quantities(self.expected_effect, "CandidateAssessment.expected_effect")
        _check_explanation(self.risk_notes, "CandidateAssessment.risk_notes")


@dataclass(frozen=True)
class NextCandidateProposal:
    """The Evidence Coordinator's proposal for the next trial.

    A proposal names a candidate and the obligations it would serve.  It does
    not reserve, schedule, charge or close anything: the Kernel decides
    whether to open a trial, and design section 8's exhaustion and budget
    rules decide whether it may.
    """

    candidate_id: str
    #: Evidence cells this trial would contribute to.
    evidence_cell_refs: Tuple[str, ...] = ()
    #: Draft estimate of information gain, for strategy comparison in the
    #: paper.  ``DRAFT``, therefore inadmissible for any runtime decision.
    expected_information_gain: Optional[TypedQuantity] = None
    rationale: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_cell_refs", tuple(self.evidence_cell_refs))
        _check_draft_quantities(
            self.expected_information_gain, "NextCandidateProposal.expected_information_gain"
        )
        _check_explanation(self.rationale, "NextCandidateProposal.rationale")


#: The closed set of advisory bodies.
AdvisoryBody = Union[IntentDraft, CandidateAssessment, NextCandidateProposal]

_BODY_FOR_KIND: Mapping[AdvisoryKind, type] = {
    AdvisoryKind.INTENT_DRAFT: IntentDraft,
    AdvisoryKind.CANDIDATE_ASSESSMENT: CandidateAssessment,
    AdvisoryKind.NEXT_CANDIDATE_PROPOSAL: NextCandidateProposal,
}

_ISSUER_FOR_KIND: Mapping[AdvisoryKind, ComponentId] = {
    AdvisoryKind.INTENT_DRAFT: ComponentId.INTENT_AGENT,
    AdvisoryKind.CANDIDATE_ASSESSMENT: ComponentId.XAPP_AGENT,
    AdvisoryKind.NEXT_CANDIDATE_PROPOSAL: ComponentId.EVIDENCE_COORDINATOR,
}


@dataclass(frozen=True)
class AdvisoryMessage:
    """One typed proposal on its way into the Kernel mailbox.

    Wrapped in a :class:`~assurance.core.envelopes.MailboxEnvelope` for
    transport; this is the content.  :attr:`epoch_hash` is carried on both:
    on the envelope so
    :func:`~assurance.core.envelopes.classify_envelope` can reject a stale
    proposal without inspecting the body, and here so the content is
    self-describing once unwrapped.

    ``issued_by`` must match the kind.  A "next candidate proposal" from the
    Intent Agent is not a routing curiosity -- it is a component acting outside
    its role, and roles are how design section 4.2 keeps the advisory surface
    small.  Note this is a *consistency* check, not authority: an advisory
    from the right component still confers nothing (see
    :mod:`assurance.core.components`).
    """

    message_id: str
    kind: AdvisoryKind
    issued_by: ComponentId
    correlation_id: str
    epoch_hash: str
    created_at: str
    body: AdvisoryBody

    def __post_init__(self) -> None:
        for name in ("message_id", "correlation_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string, got {value!r}")
        if not isinstance(self.kind, AdvisoryKind):
            raise TypeError("kind must be an AdvisoryKind member")
        if not isinstance(self.issued_by, ComponentId):
            raise TypeError("issued_by must be a ComponentId member")
        if self.issued_by not in ADVISORY_COMPONENTS:
            raise ValueError(
                f"{self.issued_by.value} is not an advisory component; "
                "only the Intent Agent, xApp Agent and Evidence Coordinator advise"
            )
        if self.issued_by is not _ISSUER_FOR_KIND[self.kind]:
            raise ValueError(
                f"{self.kind.value} is issued by "
                f"{_ISSUER_FOR_KIND[self.kind].value}, not {self.issued_by.value}"
            )
        if not isinstance(self.body, _BODY_FOR_KIND[self.kind]):
            raise TypeError(
                f"{self.kind.value} requires a "
                f"{_BODY_FOR_KIND[self.kind].__name__} body"
            )
        if not is_content_hash(self.epoch_hash):
            raise ValueError(f"epoch_hash is not a digest: {self.epoch_hash!r}")
        if not is_utc_timestamp(self.created_at):
            raise ValueError(f"created_at must be canonical UTC, got {self.created_at!r}")

    def to_canonical_dict(self) -> Dict[str, Any]:
        """Canonical wire form suitable for sealing into an envelope.

        The body is rendered by ``dataclasses.asdict``-equivalent traversal in
        the concrete serialiser; the mapping here keeps the outer identity
        fields in the design's camelCase spelling.
        """
        from dataclasses import asdict

        def _plain(value: Any) -> Any:
            if isinstance(value, TypedQuantity):
                return value.to_canonical_dict()
            if isinstance(value, Enum):
                return value.value
            if isinstance(value, Mapping):
                return {str(k): _plain(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [_plain(v) for v in value]
            return value

        return {
            "messageId": self.message_id,
            "kind": self.kind.value,
            "issuedBy": self.issued_by.value,
            "correlationId": self.correlation_id,
            "epochHash": self.epoch_hash,
            "createdAt": self.created_at,
            "body": _plain(asdict(self.body)),
        }

    def content_hash(self) -> str:
        """Digest of the advisory, matching its envelope's content hash."""
        return content_hash(self.to_canonical_dict())
