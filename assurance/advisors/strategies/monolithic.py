"""The monolithic LLM strategy: one model, one call, the whole job.

Owner lane: **KAGT**.  New file (Gate 6, task section 11 item 5).

This is the comparison arm for the proposed method, and task section 11 fixes
what must be held constant between them:

    monolithic LLM은 proposed method와 같은 foundation model과 통제된 동등
    token/tool-call budget을 사용한다.

Both halves are structural here rather than procedural.  *Same foundation
model* means the two strategies are constructed over the same
:class:`~assurance.advisors.strategies.transport.AdvisoryTransport` -- there
is no model-name parameter to set differently by accident.  *Equivalent
budget* means the same :class:`~assurance.advisors.strategies.budget.AgentBudget`
value object, metered by the same
:class:`~assurance.advisors.strategies.budget.AgentBudgetMeter` class with the
same enforcement rule;
:func:`~assurance.advisors.strategies.registry.build_comparable_llm_pair`
builds both at once so a batch plan cannot give them different ceilings.

What differs is exactly one thing: this strategy fuses the Intent, xApp and
Evidence Coordinator roles into a single prompt and a single call.  It has the
same catalog, the same evidence view, the same budget view, the same output
schema and the same validation gates.  So a difference in outcome between this
and :class:`~assurance.advisors.strategies.llm_coordinator.RoleSeparatedLLMCoordinator`
is attributable to role separation and to nothing else -- which is the only
reason the comparison is worth running.

It is *not* handicapped.  It may spend the full tool-call allowance; it simply
does not need more than one, and the meter records that difference rather than
hiding it.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from assurance.advisors.messages import AdvisoryMessage
from assurance.advisors.strategies.llm_base import (
    LLMStrategyBase,
    available_candidate_ids,
    render_budget_view,
    render_catalog_view,
    render_evidence_view,
)
from assurance.advisors.strategies.schemas import (
    MONOLITHIC_CHOICE_SCHEMA,
    require_known_candidate,
    require_known_cells,
    untrusted_text,
)
from assurance.advisors.strategy import StrategyKind

__all__ = ["MONOLITHIC_ROLE", "MonolithicLLMStrategy"]

MONOLITHIC_ROLE = "monolithic"

#: One system prompt carrying all three role descriptions.  Deliberately the
#: same *content* as the three separate ones, concatenated: if the monolithic
#: arm were told less, the experiment would be measuring prompt completeness
#: instead of coordination structure.
_MONOLITHIC_SYSTEM = (
    "You are an assurance advisor performing three roles at once: Intent Agent "
    "(read which evidence obligations are outstanding), xApp Agent (assess each "
    "available candidate for applicability, evidence served and risk), and "
    "Evidence Coordinator (choose exactly one candidate to trial next). "
    "You have no authority. You cannot add candidates, set thresholds, issue "
    "commands, assign verdicts, modify ledgers or terminate anything. "
    "Answer with a single JSON object and nothing else. "
    'Schema: {"candidateId": "<id>", "evidenceCellRefs": ["<cellId>", ...], '
    '"rationale": "<short text>"}. '
    "Only a candidate id from the available catalog, and only cell ids present in "
    "the evidence view. "
    "Never emit a confidence, a probability, a threshold or a predicted metric value: "
    "the system refuses any such field and falls back deterministically."
)


class MonolithicLLMStrategy(LLMStrategyBase):
    """Design section 12's monolithic LLM baseline."""

    strategy_kind = StrategyKind.MONOLITHIC_LLM

    def __init__(self, *, strategy_id: str = "strategy-monolithic-llm", **kwargs: Any) -> None:
        super().__init__(strategy_id=strategy_id, **kwargs)

    def _choose(
        self,
        *,
        catalog_view: Sequence[Any],
        evidence_view: Mapping[str, Any],
        budget_view: Mapping[str, Any],
        correlation_id: str,
        epoch_hash: str,
        now: str,
    ) -> Optional[AdvisoryMessage]:
        available_ids = available_candidate_ids(catalog_view)
        known_cells = tuple(sorted(evidence_view))
        prompt = (
            "Available candidates (JSON):\n"
            f"{render_catalog_view(catalog_view)}\n"
            "Evidence cells (JSON):\n"
            f"{render_evidence_view(evidence_view)}\n"
            "Kernel budget (JSON, read-only):\n"
            f"{render_budget_view(budget_view)}\n"
            "Read the obligations, assess the candidates, and choose exactly one."
        )
        completion = self._call(
            agent_role=MONOLITHIC_ROLE, system_prompt=_MONOLITHIC_SYSTEM, prompt=prompt
        )
        payload = self._parse(
            completion, schema=MONOLITHIC_CHOICE_SCHEMA, agent_role=MONOLITHIC_ROLE
        )
        candidate_id = require_known_candidate(
            payload["candidateId"], available_ids, agent_role=MONOLITHIC_ROLE
        )
        cell_refs = require_known_cells(
            payload.get("evidenceCellRefs", ()), known_cells, agent_role=MONOLITHIC_ROLE
        )
        return self._proposal(
            candidate_id=candidate_id,
            evidence_cell_refs=cell_refs,
            rationale=untrusted_text(
                payload.get("rationale", ""),
                fallback="monolithic LLM: no rationale supplied",
            ),
            correlation_id=correlation_id,
            epoch_hash=epoch_hash,
            now=now,
        )
