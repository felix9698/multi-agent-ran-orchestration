"""Design section 12's six comparable Evidence Coordinator strategies.

Owner lane: **KAGT**.  Gate 6 turned ``assurance/advisors/strategies.py`` into
this package; the two strategies that module held are unchanged in
:mod:`assurance.advisors.strategies.basic`, and ``from
assurance.advisors.strategies import DeterministicStrategy`` still resolves to
exactly the same class.

    동일 Kernel, contracts, candidate catalog, harm limits, evidence rules와
    hardware 조건 아래 다음 Evidence Coordinator strategy를 선택할 수 있게
    한다. (task section 11)

One interface, six implementations:

===========================  ==========================================  ======
Strategy kind                Class                                       Model
===========================  ==========================================  ======
``LLM_EVIDENCE_COORDINATOR``  :class:`~.llm_coordinator.RoleSeparatedLLMCoordinator`  yes
``DETERMINISTIC``             :class:`~.basic.DeterministicStrategy`      no
``RANDOM``                    :class:`~.basic.RandomStrategy`             no
``OPTIMIZATION``              :class:`~.optimization.OptimizationStrategy`  no
``MONOLITHIC_LLM``            :class:`~.monolithic.MonolithicLLMStrategy`  yes
``ADVERSARIAL_ADVISOR``       :class:`~.adversarial.AdversarialAdvisorStrategy`  no
===========================  ==========================================  ======

Everything that makes these *comparable* rather than merely coexisting lives
in the supporting modules, once each:

* :mod:`~.budget` -- the one token/tool-call/latency meter both LLM strategies
  spend from, so task section 11's "controlled equivalent budget" is enforced
  by shared code rather than asserted in two docstrings, and task section 12's
  agent token/tool-call/latency line items fall out of it.
* :mod:`~.transport` -- the advisory-only seam over
  ``decision/llm_backend.py``.  A strategy gets text in and text out and has
  no reach toward an actuator or a ledger.
* :mod:`~.schemas` -- JSON Schema validation plus the membership and
  forbidden-field checks a schema cannot make, so nothing untyped and no model
  confidence reaches an advisory.
* :mod:`~.llm_base` -- the shared metering, the four recorded failure classes
  and the deterministic fallback that ends all of them.
* :mod:`~.description` -- the frozen ``describe()`` key set that lets two
  strategies' budgets be compared as data.
* :mod:`~.registry` -- fail-closed construction from a batch plan.

The Kernel-facing contract is identical for all six: each returns a
``NEXT_CANDIDATE_PROPOSAL`` from the Evidence Coordinator naming a candidate
that is already in the frozen catalog, or ``None``.  The adversarial baseline
deliberately breaks that contract, and the system refusing it is the
experiment (design section 12: "a paper baseline, not an adversarial
code-review campaign").
"""

from __future__ import annotations

from assurance.advisors.strategies.adversarial import (
    DEFAULT_MOVE_ORDER,
    AdversarialAdvisorStrategy,
    AdversarialMove,
    MoveAttempt,
)
from assurance.advisors.strategies.basic import (
    DeterministicStrategy,
    RandomStrategy,
    available_candidates,
    budget_exhausted,
)
from assurance.advisors.strategies.budget import (
    DEFAULT_COMPARABLE_BUDGET,
    AgentBudget,
    AgentBudgetMeter,
    AgentCallRecord,
    BudgetExceeded,
)
from assurance.advisors.strategies.description import (
    STRATEGY_DESCRIPTION_KEYS,
    strategy_description,
)
from assurance.advisors.strategies.llm_base import (
    ADVISOR_PROMPT_VERSION,
    FallbackRecord,
    LLMStrategyBase,
    available_candidate_ids,
)
from assurance.advisors.strategies.llm_coordinator import (
    COORDINATOR_ROLE,
    INTENT_ROLE,
    XAPP_ROLE,
    RoleSeparatedLLMCoordinator,
)
from assurance.advisors.strategies.monolithic import MONOLITHIC_ROLE, MonolithicLLMStrategy
from assurance.advisors.strategies.optimization import (
    OBLIGATION_WEIGHTS,
    CandidateScore,
    OptimizationStrategy,
)
from assurance.advisors.strategies.registry import (
    LLM_STRATEGY_KINDS,
    STRATEGY_CLASSES,
    StrategyConfigurationError,
    build_comparable_llm_pair,
    build_strategy,
)
from assurance.advisors.strategies.schemas import (
    COORDINATOR_CHOICE_SCHEMA,
    INTENT_READING_SCHEMA,
    MONOLITHIC_CHOICE_SCHEMA,
    UNTRUSTED_TEXT_MARKER,
    XAPP_ASSESSMENT_SCHEMA,
    AdvisorySchemaError,
    untrusted_text,
)
from assurance.advisors.strategies.transport import (
    ADVISORY_TRANSPORT_SURFACE,
    AdvisoryCompletion,
    AdvisoryTransport,
    LLMBackendTransport,
    ScriptedTransport,
    TransportError,
    available_transport,
    prompt_digest,
)

__all__ = [
    "ADVISORY_TRANSPORT_SURFACE",
    "ADVISOR_PROMPT_VERSION",
    "COORDINATOR_CHOICE_SCHEMA",
    "COORDINATOR_ROLE",
    "DEFAULT_COMPARABLE_BUDGET",
    "DEFAULT_MOVE_ORDER",
    "INTENT_READING_SCHEMA",
    "INTENT_ROLE",
    "LLM_STRATEGY_KINDS",
    "MONOLITHIC_CHOICE_SCHEMA",
    "MONOLITHIC_ROLE",
    "OBLIGATION_WEIGHTS",
    "STRATEGY_CLASSES",
    "STRATEGY_DESCRIPTION_KEYS",
    "UNTRUSTED_TEXT_MARKER",
    "XAPP_ASSESSMENT_SCHEMA",
    "XAPP_ROLE",
    "AdversarialAdvisorStrategy",
    "AdversarialMove",
    "AdvisoryCompletion",
    "AdvisorySchemaError",
    "AdvisoryTransport",
    "AgentBudget",
    "AgentBudgetMeter",
    "AgentCallRecord",
    "BudgetExceeded",
    "CandidateScore",
    "DeterministicStrategy",
    "FallbackRecord",
    "LLMBackendTransport",
    "LLMStrategyBase",
    "MonolithicLLMStrategy",
    "MoveAttempt",
    "OptimizationStrategy",
    "RandomStrategy",
    "RoleSeparatedLLMCoordinator",
    "ScriptedTransport",
    "StrategyConfigurationError",
    "TransportError",
    "available_candidate_ids",
    "available_candidates",
    "available_transport",
    "budget_exhausted",
    "build_comparable_llm_pair",
    "build_strategy",
    "prompt_digest",
    "strategy_description",
    "untrusted_text",
]
