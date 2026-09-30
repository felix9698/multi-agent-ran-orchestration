"""The proposed method: a role-separated LLM Evidence Coordinator.

Owner lane: **KAGT**.  New file (Gate 6, task section 11 item 1).

Design section 12 lists this first, as "role-separated LLM Evidence
Coordinator (proposed method)".  What makes it the proposed method rather than
"an LLM in the loop" is the separation itself, and the separation has to be
real to be measurable:

* **three roles, three prompts, three calls.**  The Intent Agent role reads
  what the case still owes; the xApp Agent role assesses each available
  candidate against the deployed capabilities; the Evidence Coordinator role
  picks one.  Each is a separate transport call with its own system prompt and
  its own output schema, so a run's trace shows three attributable
  interactions rather than one opaque one.  Design section 4.2 draws those
  three roles; this strategy is what happens when a model is put behind each
  of them instead of behind the whole job.
* **each role's output is typed before the next role sees it.**  Role 2 is
  told what role 1 concluded as *validated JSON*, never as role 1's prose.
  Prose passed between roles would make the separation cosmetic -- the second
  call would be a continuation of the first with extra steps.
* **the roles can contradict each other, and that is a refusal.**  If the
  coordinator role picks a candidate its own xApp role rated inapplicable, the
  strategy falls back deterministically rather than proceeding.  A system
  where the last role can overrule the others is a monolith wearing three
  hats.

Everything a model says here is advisory and inadmissible.  The only numbers
in the output are ids that must already exist in the frozen catalog and the
case's evidence view; the only prose reaches the Operator through
:func:`~assurance.advisors.strategies.schemas.untrusted_text`, capped and
marked.  There is no confidence, no threshold and no predicted effect used for
anything -- the constraint list forbids it, and the schemas have nowhere to
put it.

Hardware-free: the strategy touches a transport and nothing else.  Under
:class:`~assurance.advisors.strategies.transport.ScriptedTransport` it makes
no network call at all, which is how the whole test matrix runs hermetically.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from assurance.advisors.messages import AdvisoryMessage
from assurance.advisors.strategies.llm_base import (
    LLMStrategyBase,
    available_candidate_ids,
    render_budget_view,
    render_catalog_view,
    render_evidence_view,
)
from assurance.advisors.strategies.schemas import (
    COORDINATOR_CHOICE_SCHEMA,
    INTENT_READING_SCHEMA,
    XAPP_ASSESSMENT_SCHEMA,
    AdvisorySchemaError,
    require_known_candidate,
    require_known_cells,
    untrusted_text,
)
from assurance.advisors.strategy import StrategyKind
from assurance.core.axes import OPEN_EVIDENCE_CELL_STATUSES

#: The four statuses that still owe evidence, by name.  Taken from the axis
#: enum rather than written out, so a status added to
#: :class:`~assurance.core.axes.EvidenceCellStatus` cannot silently stop
#: counting as an obligation here.
_OPEN_STATUS_NAMES = frozenset(status.value for status in OPEN_EVIDENCE_CELL_STATUSES)

__all__ = [
    "COORDINATOR_ROLE",
    "INTENT_ROLE",
    "RoleSeparatedLLMCoordinator",
    "XAPP_ROLE",
]

INTENT_ROLE = "intent-agent"
XAPP_ROLE = "xapp-agent"
COORDINATOR_ROLE = "evidence-coordinator"

#: The rules every role is given.  Repeated in each system prompt rather than
#: stated once in a shared preamble, because each call is independent -- a
#: model answering role 3 has not seen role 1's instructions.
_COMMON_RULES = (
    "You are one advisory role in an assurance system. You have no authority. "
    "You cannot add candidates, set thresholds, issue commands, assign verdicts, "
    "modify ledgers or terminate anything. "
    "Answer with a single JSON object and nothing else. "
    "Never emit a confidence, a probability, a threshold or a predicted metric value: "
    "the system refuses any such field and falls back deterministically."
)

_INTENT_SYSTEM = (
    f"{_COMMON_RULES} "
    "Your role is Intent Agent: read the case's evidence cells and name which "
    "obligations are still outstanding. "
    'Schema: {"outstandingCells": ["<cellId>", ...], "note": "<short text>"}. '
    "Only cell ids present in the view, and never a sealed cell."
)

_XAPP_SYSTEM = (
    f"{_COMMON_RULES} "
    "Your role is xApp Agent: assess each available catalog candidate for "
    "applicability, the evidence it could serve, and its risk. "
    'Schema: {"assessments": [{"candidateId": "<id>", "applicable": true|false, '
    '"evidenceNeeds": ["<cellId>", ...], "riskNote": "<short text>"}]}. '
    "Only candidate ids from the available catalog. "
    "Rating everything applicable is a catalog sweep with extra steps; be honest."
)

_COORDINATOR_SYSTEM = (
    f"{_COMMON_RULES} "
    "Your role is Evidence Coordinator: choose exactly one candidate to trial next. "
    'Schema: {"candidateId": "<id>", "evidenceCellRefs": ["<cellId>", ...], '
    '"rationale": "<short text>"}. '
    "Choose only from candidates the xApp assessment marked applicable."
)


def _outstanding_cells(evidence_view: Mapping[str, Any]) -> Tuple[str, ...]:
    """Unsealed cells the case still owes, sorted.

    Sealed cells are excluded outright: design section 8 keeps dormant
    evidence sealed until its target vector is released, so an obligation a
    role could name has to be one the experiment has actually opened.
    """
    out: List[str] = []
    for cell_id in sorted(evidence_view):
        cell = evidence_view[cell_id]
        if not isinstance(cell, Mapping):
            continue
        if cell.get("sealed"):
            continue
        if str(cell.get("status", "")) in _OPEN_STATUS_NAMES:
            out.append(cell_id)
    return tuple(out)


class RoleSeparatedLLMCoordinator(LLMStrategyBase):
    """Design section 12's proposed method.

    Three calls per proposal by construction, which is also why the default
    :data:`~assurance.advisors.strategies.budget.DEFAULT_COMPARABLE_BUDGET`
    allows four: the ceiling has to fit the method that spends the most, or
    the "controlled equivalent budget" would be a handicap dressed as a
    control.
    """

    strategy_kind = StrategyKind.LLM_EVIDENCE_COORDINATOR

    def __init__(self, *, strategy_id: str = "strategy-llm-evidence-coordinator", **kwargs: Any) -> None:
        super().__init__(strategy_id=strategy_id, **kwargs)

    # -- the three roles ---------------------------------------------------

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
        catalog_json = render_catalog_view(catalog_view)
        evidence_json = render_evidence_view(evidence_view)
        budget_json = render_budget_view(budget_view)

        reading = self._read_intent(
            evidence_json=evidence_json,
            budget_json=budget_json,
            known_cells=known_cells,
            outstanding=_outstanding_cells(evidence_view),
        )
        assessments = self._assess_candidates(
            catalog_json=catalog_json,
            evidence_json=evidence_json,
            reading=reading,
            available_ids=available_ids,
            known_cells=known_cells,
        )
        return self._coordinate(
            catalog_json=catalog_json,
            budget_json=budget_json,
            reading=reading,
            assessments=assessments,
            available_ids=available_ids,
            known_cells=known_cells,
            correlation_id=correlation_id,
            epoch_hash=epoch_hash,
            now=now,
        )

    def _read_intent(
        self,
        *,
        evidence_json: str,
        budget_json: str,
        known_cells: Sequence[str],
        outstanding: Sequence[str],
    ) -> Dict[str, Any]:
        prompt = (
            "Evidence cells (JSON):\n"
            f"{evidence_json}\n"
            "Kernel budget (JSON, read-only):\n"
            f"{budget_json}\n"
            "Cells the Kernel already reports as unsealed and open:\n"
            f"{list(outstanding)}\n"
            "Name the outstanding obligations."
        )
        completion = self._call(agent_role=INTENT_ROLE, system_prompt=_INTENT_SYSTEM, prompt=prompt)
        payload = self._parse(completion, schema=INTENT_READING_SCHEMA, agent_role=INTENT_ROLE)
        payload["outstandingCells"] = list(
            require_known_cells(payload.get("outstandingCells", ()), known_cells, agent_role=INTENT_ROLE)
        )
        return payload

    def _assess_candidates(
        self,
        *,
        catalog_json: str,
        evidence_json: str,
        reading: Mapping[str, Any],
        available_ids: Sequence[str],
        known_cells: Sequence[str],
    ) -> Dict[str, Any]:
        # Role 1 reaches role 2 as validated structure, never as its prose.
        prompt = (
            "Available candidates (JSON):\n"
            f"{catalog_json}\n"
            "Evidence cells (JSON):\n"
            f"{evidence_json}\n"
            "Outstanding obligations named by the Intent Agent role:\n"
            f"{list(reading.get('outstandingCells', ()))}\n"
            "Assess every available candidate."
        )
        completion = self._call(agent_role=XAPP_ROLE, system_prompt=_XAPP_SYSTEM, prompt=prompt)
        payload = self._parse(completion, schema=XAPP_ASSESSMENT_SCHEMA, agent_role=XAPP_ROLE)
        for assessment in payload["assessments"]:
            require_known_candidate(
                assessment["candidateId"], available_ids, agent_role=XAPP_ROLE
            )
            assessment["evidenceNeeds"] = list(
                require_known_cells(
                    assessment.get("evidenceNeeds", ()), known_cells, agent_role=XAPP_ROLE
                )
            )
        return payload

    def _coordinate(
        self,
        *,
        catalog_json: str,
        budget_json: str,
        reading: Mapping[str, Any],
        assessments: Mapping[str, Any],
        available_ids: Sequence[str],
        known_cells: Sequence[str],
        correlation_id: str,
        epoch_hash: str,
        now: str,
    ) -> AdvisoryMessage:
        applicable: Set[str] = {
            item["candidateId"] for item in assessments["assessments"] if item["applicable"]
        }
        if not applicable:
            # Every available candidate rated inapplicable is a degenerate
            # verdict, not a starting point for a choice.  Asking the
            # coordinator role to pick from an empty set is how the
            # consistency check below gets skipped: with nothing applicable
            # there is nothing for a choice to contradict, and any candidate
            # would pass.  So the empty set is refused here, before the third
            # call is made, and the deterministic fallback proposes instead.
            #
            # Falling back rather than returning ``None`` is the same rule the
            # rest of this package follows: silence would read as exhaustion,
            # and exhaustion is the Kernel's decision from evidence cells
            # (design section 6.4), never a proposer running out of ideas.
            raise AdvisorySchemaError(
                "no-applicable-candidate",
                f"{XAPP_ROLE} rated all {len(assessments['assessments'])} assessed "
                "candidate(s) inapplicable; there is nothing for the "
                f"{COORDINATOR_ROLE} role to choose between",
                agent_role=XAPP_ROLE,
            )
        prompt = (
            "Available candidates (JSON):\n"
            f"{catalog_json}\n"
            "Kernel budget (JSON, read-only):\n"
            f"{budget_json}\n"
            "Outstanding obligations (Intent Agent role):\n"
            f"{list(reading.get('outstandingCells', ()))}\n"
            "Assessments (xApp Agent role):\n"
            f"{sorted(applicable)}\n"
            "Choose exactly one candidate to trial next."
        )
        completion = self._call(
            agent_role=COORDINATOR_ROLE, system_prompt=_COORDINATOR_SYSTEM, prompt=prompt
        )
        payload = self._parse(completion, schema=COORDINATOR_CHOICE_SCHEMA, agent_role=COORDINATOR_ROLE)

        candidate_id = require_known_candidate(
            payload["candidateId"], available_ids, agent_role=COORDINATOR_ROLE
        )
        if candidate_id not in applicable:
            # The roles disagreed.  Refusing here is what keeps the separation
            # real: a coordinator role that can overrule its own xApp role is
            # a monolith with three prompts.
            #
            # Unconditional: an ``if applicable and ...`` guard here would
            # make the check skippable by the xApp role rating everything
            # inapplicable, which is why the empty set is refused above
            # instead of being allowed to reach this line.
            raise AdvisorySchemaError(
                "contradiction",
                f"{COORDINATOR_ROLE} chose {candidate_id!r}, which the {XAPP_ROLE} role "
                f"did not rate applicable ({sorted(applicable)})",
                agent_role=COORDINATOR_ROLE,
            )
        cell_refs = require_known_cells(
            payload.get("evidenceCellRefs", ()), known_cells, agent_role=COORDINATOR_ROLE
        )
        return self._proposal(
            candidate_id=candidate_id,
            evidence_cell_refs=cell_refs,
            rationale=untrusted_text(
                payload.get("rationale", ""),
                fallback="role-separated LLM Evidence Coordinator: no rationale supplied",
            ),
            correlation_id=correlation_id,
            epoch_hash=epoch_hash,
            now=now,
        )
