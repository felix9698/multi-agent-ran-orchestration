"""The three advisory agents (design section 4.2).

Owner lane: **KAGT** for :mod:`assurance.advisors.roles` and
:mod:`assurance.advisors.strategy`; :mod:`assurance.advisors.messages` is
complete and has no owner.

Intent Agent, xApp Agent(s) and Evidence Coordinator.  All advisory: they emit
typed proposals into the Kernel mailbox and own nothing.  They cannot add
candidates, change targets, issue actuator commands, assign verdicts, modify
ledgers, release a target vector or terminate a case, and they have no direct
actuator tools.

The strategy interface in :mod:`assurance.advisors.strategy` is the single
seam design section 12's six comparable strategies plug into, plus the
deterministic fallback that keeps a case finite when an agent times out or
returns malformed output.

Gate 2 also ships, in files KAGT added at its own discretion (see
``docs/architecture/SEAMS-GATE2.md`` section 3):

* :mod:`assurance.advisors.mailbox` -- the typed mailbox sending contract and
  a sender-side violation-sample factory (task section 6.12).
* :mod:`assurance.advisors.grammar` and
  :mod:`assurance.advisors.deterministic_agents` -- LLM-free, deterministic
  Intent Agent and xApp Agent implementations.
* :mod:`assurance.advisors.catalog_view` and
  :mod:`assurance.advisors.proposal_support` -- the calling convention for a
  ``catalog_view`` and the shared building blocks every proposal producer uses.
* :mod:`assurance.advisors.coordinator` -- the Evidence Coordinator role,
  implemented once as a strategy-backed caller that applies the timeout and
  deterministic-fallback rule design sections 8, 12 and 15 require.

Gate 6 completed the family.  :mod:`assurance.advisors.strategies`, which
Gate 2 left holding DETERMINISTIC and RANDOM, is now the package carrying all
six of design section 12's comparable strategies -- the role-separated LLM
Evidence Coordinator (the proposed method), the monolithic LLM comparison
under a controlled equivalent budget, the optimization-based strategy and the
adversarial Advisor baseline -- together with the shared token/tool-call/
latency meter, the advisory-only LLM transport seam and the JSON-schema gates
that keep model output typed.  Its ``__init__`` is the map.
"""

from __future__ import annotations

from assurance.advisors.catalog_view import CatalogEntry
from assurance.advisors.action_space import (
    AdvisoryAction, advisory_action_space, resolve_composition,
)
from assurance.advisors.coordinator import FallbackEvent, StrategyBackedEvidenceCoordinator
from assurance.advisors.deterministic_agents import DeterministicIntentAgent, RuleBasedXAppAgent
from assurance.advisors.grammar import IntentGrammarEntry, IntentParseError
from assurance.advisors.mailbox import (
    SENDER_VIOLATION_KINDS,
    MailboxViolationSample,
    build_violation_sample,
    seal_advisory_message,
)
from assurance.advisors.messages import (
    FORBIDDEN_ADVISORY_FIELDS,
    MAX_EXPLANATION_CHARS,
    AdvisoryBody,
    AdvisoryKind,
    AdvisoryMessage,
    CandidateAssessment,
    IntentDraft,
    NextCandidateProposal,
)
from assurance.advisors.roles import EvidenceCoordinator, IntentAgent, XAppAgent
from assurance.advisors.strategies import (
    AdversarialAdvisorStrategy,
    DeterministicStrategy,
    MonolithicLLMStrategy,
    OptimizationStrategy,
    RandomStrategy,
    RoleSeparatedLLMCoordinator,
    build_strategy,
)
from assurance.advisors.strategy import (
    AdvisoryStrategy,
    StrategyKind,
    deterministic_fallback,
)

__all__ = [
    "FORBIDDEN_ADVISORY_FIELDS",
    "MAX_EXPLANATION_CHARS",
    "SENDER_VIOLATION_KINDS",
    "AdvisoryBody",
    "AdvisoryAction",
    "AdvisoryKind",
    "AdvisoryMessage",
    "AdversarialAdvisorStrategy",
    "AdvisoryStrategy",
    "CandidateAssessment",
    "CatalogEntry",
    "DeterministicIntentAgent",
    "DeterministicStrategy",
    "EvidenceCoordinator",
    "FallbackEvent",
    "IntentAgent",
    "IntentDraft",
    "IntentGrammarEntry",
    "IntentParseError",
    "MailboxViolationSample",
    "MonolithicLLMStrategy",
    "NextCandidateProposal",
    "OptimizationStrategy",
    "RandomStrategy",
    "RoleSeparatedLLMCoordinator",
    "RuleBasedXAppAgent",
    "StrategyBackedEvidenceCoordinator",
    "StrategyKind",
    "XAppAgent",
    "build_strategy",
    "advisory_action_space",
    "build_violation_sample",
    "deterministic_fallback",
    "resolve_composition",
    "seal_advisory_message",
]
