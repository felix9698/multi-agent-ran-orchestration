"""What the two LLM strategies share: views rendered as prompts, one metered
call, and the deterministic fallback that ends every failure.

Owner lane: **KAGT**.  New file (Gate 6, task section 11).

Task section 11's last comparison condition is the one this module carries:

    agent timeout, malformed output, hallucinated candidate와 exhausted
    opportunity에서 **deterministic fallback으로 유한 종료**한다.

Two things enforce that, at different distances.
:class:`~assurance.advisors.coordinator.StrategyBackedEvidenceCoordinator`
bounds a strategy from outside with a wall-clock timeout, and cannot see
*why* a strategy failed -- only that it did.  This module bounds it from
inside, where the reason is known: a transport error, a schema violation, a
hallucinated candidate id and an exhausted token budget are four different
recorded causes, and task section 12 wants "fallback usage" broken down by
which.  Both layers end in the same
:func:`~assurance.advisors.strategy.deterministic_fallback`, so a run in which
the model never answered is as reproducible as one in which it always did.

The prompt rendering is deliberately mechanical.  Every view a strategy
receives is turned into canonical JSON with sorted keys, so the same case
state produces the same prompt bytes on every host -- which is what makes
:func:`~assurance.advisors.strategies.transport.prompt_digest` a stable
provenance record rather than a per-run curiosity.  It is also why the views
are rendered rather than summarised: a summary would be a place for this
module to make a decision, and the decision being measured belongs to the
model.

Nothing here reaches an actuator, a ledger or a Kernel.  A strategy is
constructed with a transport and a budget; there is no parameter for anything
else, which is the structural half of "advisory" (design section 4.2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from assurance.advisors.messages import AdvisoryMessage
from assurance.advisors.proposal_support import (
    available_candidates,
    budget_exhausted,
    build_next_candidate_proposal,
)
from assurance.advisors.strategies.budget import (
    DEFAULT_COMPARABLE_BUDGET,
    AgentBudget,
    AgentBudgetMeter,
    BudgetExceeded,
)
from assurance.advisors.strategies.description import strategy_description
from assurance.advisors.strategies.schemas import AdvisorySchemaError, parse_role_output
from assurance.advisors.strategies.transport import (
    AdvisoryCompletion,
    AdvisoryTransport,
    TransportError,
)
from assurance.advisors.strategy import deterministic_fallback

__all__ = [
    "ADVISOR_PROMPT_VERSION",
    "FallbackRecord",
    "LLMStrategyBase",
    "available_candidate_ids",
    "render_budget_view",
    "render_catalog_view",
    "render_evidence_view",
]

def _stamped(error: Exception, agent_role: str) -> Exception:
    """Attach the advisory role that was speaking when *error* was raised.

    A budget or transport failure knows *what* ran out but not *who* was
    asking, and only the call site knows that.  Task section 12 wants
    fallback usage broken down, and a breakdown whose role column always said
    "llm" would answer a question nobody asked.
    """
    setattr(error, "agent_role", agent_role)
    return error


def _role_of(error: Exception) -> str:
    return str(getattr(error, "agent_role", "") or "llm")


#: Version stamped into every strategy description.  Design section 12 wants
#: "prompt version" in the run record, and a comparison whose prompts changed
#: mid-batch without the version moving would be unreproducible.
ADVISOR_PROMPT_VERSION = "assurance-advisor-prompts/1.0.0"


@dataclass(frozen=True)
class FallbackRecord:
    """One in-strategy activation of the deterministic fallback.

    Distinct from
    :class:`~assurance.advisors.coordinator.FallbackEvent`, which records the
    coordinator's *outside* view (timeout, exception, malformed return).  This
    one carries the cause the strategy could see from inside, which is the
    breakdown task section 12 asks for.
    """

    correlation_id: str
    #: ``transport`` | ``budget`` | one of
    #: :attr:`~assurance.advisors.strategies.schemas.AdvisorySchemaError.reason`.
    cause: str
    detail: str
    #: Which advisory role was speaking.  Spelled ``agent_role`` and never
    #: ``role``: the seam test in ``tests/assurance/test_seams.py`` bans the
    #: bare identifier across this package, because design section 5 removes
    #: human identity and role from the record entirely, and a field named
    #: ``role`` is exactly where one would come back.
    agent_role: str

    def to_canonical_dict(self) -> Dict[str, Any]:
        return {
            "correlationId": self.correlation_id,
            "cause": self.cause,
            "detail": self.detail,
            "agentRole": self.agent_role,
        }


# --------------------------------------------------------------------------- #
# View rendering
# --------------------------------------------------------------------------- #


def render_catalog_view(catalog_view: Sequence[Any]) -> str:
    """The available candidates, as canonical JSON.

    Only ``AVAILABLE`` entries are rendered.  A model shown a candidate the
    Kernel has locked would be being invited to name it, and naming it would
    be refused downstream -- so the prompt states the true option set and the
    refusal stays a genuine hallucination check rather than a trap.
    """
    rows: List[Dict[str, Any]] = []
    for candidate in available_candidates(catalog_view):
        rows.append(
            {
                "candidateId": candidate.candidate_id,
                "targetRef": candidate.target_ref,
                "optionRef": candidate.option_ref,
                "capabilityRef": candidate.capability_ref,
                "parameters": {str(k): str(v) for k, v in dict(candidate.parameters).items()},
            }
        )
    rows.sort(key=lambda row: row["candidateId"])
    return json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def render_evidence_view(evidence_view: Mapping[str, Any]) -> str:
    """The case's evidence cells, sealed ones included but never opened.

    A sealed cell is rendered as sealed and without its contents, exactly as
    the Kernel handed it over: design section 8 keeps dormant evidence sealed
    until its target vector is active, and a prompt that leaked the
    contributions would let a strategy plan against information the experiment
    has not released.
    """
    rows: List[Dict[str, Any]] = []
    for cell_id in sorted(evidence_view):
        cell = evidence_view[cell_id]
        if isinstance(cell, Mapping):
            rows.append(
                {
                    "cellId": cell_id,
                    "status": str(cell.get("status", "UNKNOWN")),
                    "targetRef": str(cell.get("targetRef", "")),
                    "sealed": bool(cell.get("sealed", False)),
                }
            )
        else:
            rows.append({"cellId": cell_id, "status": str(cell), "targetRef": "", "sealed": False})
    return json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def render_budget_view(budget_view: Mapping[str, Any]) -> str:
    """The Kernel's finite limits, read-only and rendered verbatim."""
    plain = {str(key): budget_view[key] for key in sorted(budget_view)}
    return json.dumps(plain, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


# --------------------------------------------------------------------------- #
# The shared strategy body
# --------------------------------------------------------------------------- #


class LLMStrategyBase:
    """Metering, fallback and telemetry for a model-backed strategy.

    Subclasses supply :meth:`_choose`, which may make one metered call or
    several.  Everything around it -- opening the budget window, catching the
    four failure classes, recording why, and producing the deterministic
    fallback -- happens here, identically for both strategies.  That sameness
    is the point: task section 11 requires the monolithic comparison to differ
    from the proposed method in *how it coordinates*, not in how carefully it
    is wrapped.
    """

    strategy_kind: Any = None

    def __init__(
        self,
        *,
        transport: AdvisoryTransport,
        budget: Optional[AgentBudget] = None,
        meter: Optional[AgentBudgetMeter] = None,
        strategy_id: str = "strategy-llm",
        prompt_version: str = ADVISOR_PROMPT_VERSION,
        temperature: Optional[float] = 0.0,
    ) -> None:
        if not hasattr(transport, "complete") or not hasattr(transport, "describe"):
            raise TypeError("transport must implement the AdvisoryTransport surface")
        if meter is not None and budget is not None and meter.budget != budget:
            raise ValueError(
                "meter and budget disagree; a strategy has exactly one ceiling "
                "(task section 11's controlled equivalent budget)"
            )
        self.strategy_id = str(strategy_id)
        self._transport = transport
        self._meter = meter if meter is not None else AgentBudgetMeter(
            budget if budget is not None else DEFAULT_COMPARABLE_BUDGET,
            meter_id=f"{strategy_id}/meter",
        )
        self._prompt_version = str(prompt_version)
        self._temperature = temperature
        self._fallbacks: List[FallbackRecord] = []
        self._proposals = 0

    # -- read-only observation --------------------------------------------

    @property
    def meter(self) -> AgentBudgetMeter:
        return self._meter

    @property
    def fallbacks(self) -> Tuple[FallbackRecord, ...]:
        return tuple(self._fallbacks)

    def telemetry(self) -> Mapping[str, Any]:
        """The §12 line items this strategy produced, ready for the run record."""
        return {
            "strategyId": self.strategy_id,
            "strategyKind": getattr(self.strategy_kind, "value", self.strategy_kind),
            "proposals": self._proposals,
            "totals": dict(self._meter.cumulative_totals()),
            "calls": [record.to_canonical_dict() for record in self._meter.records],
            "fallbacks": [record.to_canonical_dict() for record in self._fallbacks],
        }

    def describe(self) -> Mapping[str, Any]:
        budget = self._meter.budget
        return strategy_description(
            strategy_id=self.strategy_id,
            strategy_kind=self.strategy_kind,
            model_identity=str(getattr(self._transport, "model_identity", "")) or None,
            prompt_version=self._prompt_version,
            token_budget=budget.max_total_tokens,
            tool_call_budget=budget.max_tool_calls,
            latency_budget_ms=budget.max_latency_ms,
            seed=None,
            temperature=self._temperature,
            extra={"transport": dict(self._transport.describe())},
        )

    # -- the frozen interface ---------------------------------------------

    def propose(
        self,
        *,
        catalog_view: Sequence[Any],
        evidence_view: Mapping[str, Any],
        budget_view: Mapping[str, Any],
        correlation_id: str,
        epoch_hash: str,
        now: str,
    ) -> Optional[AdvisoryMessage]:
        """Ask the model, or fall back deterministically.

        Returns ``None`` only when there is genuinely nothing to propose -- an
        exhausted Kernel budget or an empty available set.  ``None`` is never
        an exhaustion certificate; that remains the Kernel's decision from
        evidence cells (design section 6.4).
        """
        self._proposals += 1
        if budget_exhausted(budget_view):
            return None
        available = available_candidates(catalog_view)
        if not available:
            return None

        self._meter.begin_proposal(correlation_id)
        try:
            return self._choose(
                catalog_view=catalog_view,
                evidence_view=evidence_view,
                budget_view=budget_view,
                correlation_id=correlation_id,
                epoch_hash=epoch_hash,
                now=now,
            )
        except AdvisorySchemaError as exc:
            return self._fallback(
                cause=exc.reason,
                detail=exc.detail,
                agent_role=exc.agent_role or "llm",
                catalog_view=catalog_view,
                evidence_view=evidence_view,
                budget_view=budget_view,
                correlation_id=correlation_id,
                epoch_hash=epoch_hash,
                now=now,
            )
        except BudgetExceeded as exc:
            return self._fallback(
                cause="budget",
                # The reason belongs in the detail: task section 12's
                # fallback breakdown has to tell "spent too much" from "could
                # not be priced at all", and the two mean different things
                # about whether the ceiling held.
                detail=f"{exc.dimension} {exc.reason}: {exc.used} of {exc.limit}",
                agent_role=_role_of(exc),
                catalog_view=catalog_view,
                evidence_view=evidence_view,
                budget_view=budget_view,
                correlation_id=correlation_id,
                epoch_hash=epoch_hash,
                now=now,
            )
        except TransportError as exc:
            return self._fallback(
                cause="transport",
                detail=str(exc),
                agent_role=_role_of(exc),
                catalog_view=catalog_view,
                evidence_view=evidence_view,
                budget_view=budget_view,
                correlation_id=correlation_id,
                epoch_hash=epoch_hash,
                now=now,
            )

    # -- subclass hook -----------------------------------------------------

    def _choose(self, **kwargs: Any) -> Optional[AdvisoryMessage]:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- shared internals --------------------------------------------------

    def _call(self, *, agent_role: str, system_prompt: str, prompt: str) -> AdvisoryCompletion:
        """One metered model call.

        The tool-call allowance is checked *before* the transport is touched,
        so a strategy out of budget makes no call it cannot pay for; the
        tokens it actually spent are charged after, because they are only
        known then.

        Any exception a transport raises becomes a
        :class:`~assurance.advisors.strategies.transport.TransportError` here,
        including one from a transport that did not honour the contract.
        :class:`~assurance.advisors.coordinator.StrategyBackedEvidenceCoordinator`
        would catch a stray exception anyway and the case would still
        terminate -- but it would be recorded as an anonymous strategy failure
        rather than as a provider failure, and task section 12 wants the
        breakdown.
        """
        try:
            self._meter.ensure_room(tool_calls=1)
        except BudgetExceeded as exc:
            raise _stamped(exc, agent_role)
        try:
            completion = self._transport.complete(system_prompt=system_prompt, prompt=prompt)
        except TransportError as exc:
            raise _stamped(exc, agent_role)
        except Exception as exc:  # noqa: BLE001 - a provider failure is one type here
            raise _stamped(TransportError(f"{agent_role}: {exc!r}"), agent_role) from exc
        try:
            self._meter.charge(
                agent_role=agent_role,
                model_identity=completion.model_identity,
                prompt_hash=completion.prompt_hash,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                latency_ms=completion.latency_ms,
                tool_calls=1,
            )
        except BudgetExceeded as exc:
            raise _stamped(exc, agent_role)
        return completion

    def _parse(self, completion: AdvisoryCompletion, *, schema: Mapping[str, Any], agent_role: str):
        try:
            return parse_role_output(completion.text, schema=schema, agent_role=agent_role)
        except AdvisorySchemaError as exc:
            exc.agent_role = exc.agent_role or agent_role
            raise

    def _fallback(
        self,
        *,
        cause: str,
        detail: str,
        agent_role: str,
        catalog_view: Sequence[Any],
        evidence_view: Mapping[str, Any],
        budget_view: Mapping[str, Any],
        correlation_id: str,
        epoch_hash: str,
        now: str,
    ) -> Optional[AdvisoryMessage]:
        self._fallbacks.append(
            FallbackRecord(
                correlation_id=correlation_id, cause=cause, detail=detail, agent_role=agent_role
            )
        )
        return deterministic_fallback(
            catalog_view=catalog_view,
            evidence_view=evidence_view,
            budget_view=budget_view,
            correlation_id=correlation_id,
            epoch_hash=epoch_hash,
            now=now,
            failure_reason=f"{self.strategy_id}/{agent_role}: {cause}",
        )

    def _proposal(
        self,
        *,
        candidate_id: str,
        evidence_cell_refs: Sequence[str],
        rationale: str,
        correlation_id: str,
        epoch_hash: str,
        now: str,
    ) -> AdvisoryMessage:
        message = build_next_candidate_proposal(
            message_id=f"{self.strategy_id}/{correlation_id}/{self._proposals}",
            candidate_id=candidate_id,
            rationale=rationale,
            correlation_id=correlation_id,
            epoch_hash=epoch_hash,
            now=now,
        )
        if not evidence_cell_refs:
            return message
        return replace(
            message, body=replace(message.body, evidence_cell_refs=tuple(evidence_cell_refs))
        )


def available_candidate_ids(catalog_view: Sequence[Any]) -> Tuple[str, ...]:
    """Available candidate ids, sorted -- the membership set every check uses.

    Sorted rather than in catalog order, because it is used both to build a
    prompt and to check what came back, and the two must agree regardless of
    how the caller assembled the sequence.
    """
    return tuple(sorted(candidate.candidate_id for candidate in available_candidates(catalog_view)))
