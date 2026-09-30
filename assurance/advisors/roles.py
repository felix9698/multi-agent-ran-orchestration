"""The three advisory role protocols.

Owner lane: **KAGT**.  Signatures frozen by this design step.

Design section 4.2 defines the roles and the shared limit:

    **Intent Agent:** turns natural language into a typed contract draft and
    produces Operator-facing explanations.
    **xApp Agent(s):** assess candidate applicability, expected effect, risk,
    and evidence needs for registered xApp capabilities.
    **Evidence Coordinator:** proposes the next catalog candidate and evidence
    obligations under the current Kernel policy mode.

    All three are advisory. ... Their messages pass through a typed Kernel
    mailbox.  They have no direct actuator tools.

Every method below returns an
:class:`~assurance.advisors.messages.AdvisoryMessage` and nothing else.  There
is no method that applies, reserves, charges, closes or terminates, and there
is no handle through which one could be reached: an implementation is
constructed with read-only views and a submit callback, never with the Kernel,
the Write Gateway, an R1 client or a ledger.

The observation the interfaces encode: an agent is given *what is already
frozen* and asked for an opinion about it.  It receives the catalog rather than
producing one, the evidence state rather than writing it, and the contracts
rather than editing them.  An LLM behind one of these methods is a text
transformer with a typed output schema -- design section 15 requires a
"deterministic fallback after agent timeout or malformed output", which is
only meaningful because nothing downstream depends on the agent succeeding.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from assurance.advisors.messages import AdvisoryMessage

__all__ = ["EvidenceCoordinator", "IntentAgent", "XAppAgent"]


@runtime_checkable
class IntentAgent(Protocol):
    """Natural language in, typed contract draft out."""

    def draft_contract(
        self,
        *,
        utterance: str,
        objective_registry: Mapping[str, Any],
        capability_view: Sequence[Any],
        correlation_id: str,
        epoch_hash: str,
        now: str,
    ) -> AdvisoryMessage:
        """Produce an
        :class:`~assurance.advisors.messages.IntentDraft` advisory.

        Signature frozen; body owned by lane **KAGT**.

        *objective_registry* and *capability_view* are read-only: the agent
        selects an objective family that already exists and constraints over
        measurements that already exist.  It cannot introduce either, so an
        intent the deployment cannot serve comes back in
        :attr:`~assurance.advisors.messages.IntentDraft.unsupported_requests`
        rather than as an invented contract (design section 10 requires
        unsupported fields to fail closed and stay honestly advertised).

        Every proposed bound must be a ``DRAFT``
        :class:`~assurance.core.provenance.TypedQuantity`; the message type
        enforces it, and the effect is that a drafted threshold is
        inadmissible until the Operator confirms it.

        Must raise or return a well-formed message.  A partially-formed
        advisory is not an option: malformed output is precisely what the
        deterministic fallback in
        :func:`assurance.advisors.strategy.deterministic_fallback` exists for.
        """
        ...

    def explain(self, *, subject_ref: str, state_view: Mapping[str, Any]) -> str:
        """Operator-facing explanation of *subject_ref*.

        Signature frozen; body owned by lane **KAGT**.

        Display data, capped at
        :data:`~assurance.advisors.messages.MAX_EXPLANATION_CHARS`.  It is
        never parsed, never an input to a decision, and must be labelled
        untrusted wherever the Cockpit renders it -- design section 11 keeps
        LIVE, REPLAY, DERIVED, UNKNOWN and UNSUPPORTED visibly distinct, and
        model prose belongs on the same honest footing.
        """
        ...


@runtime_checkable
class XAppAgent(Protocol):
    """Assesses what a registered capability can do for a candidate."""

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
        """Produce a
        :class:`~assurance.advisors.messages.CandidateAssessment` advisory.

        Signature frozen; body owned by lane **KAGT**.

        *candidate* comes from the frozen catalog.  The agent assesses it; it
        cannot propose a different one, parameterise it differently, or
        construct a policy body -- building an actuator payload is the Write
        Gateway's exclusive job (design section 4.4), and that separation is
        the cutover for GAP-01 in ``docs/architecture/GATE1-MAP.md``.

        ``applicable=False`` is a useful answer and must be returned honestly.
        An agent that rates everything applicable produces a catalog sweep
        with extra steps.
        """
        ...


@runtime_checkable
class EvidenceCoordinator(Protocol):
    """Proposes which candidate to try next, and why.

    Replaceable through the single interface in
    :mod:`assurance.advisors.strategy` -- design section 12 lists six
    strategies (role-separated LLM, deterministic, random, optimisation-based,
    monolithic LLM, adversarial) that "use the same Kernel, contracts,
    catalog, harm limits, hardware conditions, and evidence rules".  This
    protocol is what they all implement, so swapping the strategy changes the
    proposal and nothing else.
    """

    def propose_next(
        self,
        *,
        catalog_view: Sequence[Any],
        evidence_view: Mapping[str, Any],
        budget_view: Mapping[str, Any],
        correlation_id: str,
        epoch_hash: str,
        now: str,
    ) -> Optional[AdvisoryMessage]:
        """Produce a
        :class:`~assurance.advisors.messages.NextCandidateProposal`, or
        ``None`` when the coordinator has nothing to propose.

        Signature frozen; body owned by lane **KAGT**.

        ``None`` is a legitimate answer and is *not* the same as exhaustion.
        Whether the vector is exhausted is decided by the Kernel from the
        evidence cells (design section 6.4,
        :meth:`~assurance.kernel.kernel.AssuranceKernel.exhaustion_certificate`);
        an agent falling silent must never be readable as a certificate,
        because that is how a case would terminate as "explored" without
        anything being observed.

        *evidence_view* must already have dormant cells sealed by the Kernel
        (design section 8): a coordinator that could see evidence for a
        not-yet-active target vector would plan against information the
        experiment has not released.

        *budget_view* is read-only.  The deadline, trial cap, proposal cap and
        usable harm reserve are the Kernel's; design section 8: "No agent can
        keep a case alive past" any of them.
        """
        ...
