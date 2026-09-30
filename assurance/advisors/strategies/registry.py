"""Building any of the six strategies from a run configuration, fail-closed.

Owner lane: **KAGT**.  New file (Gate 6, task sections 11 and 12).

Task section 12 says a Batch Experiment configures "strategy, repeat count and
seed" among other things -- that is, a strategy is chosen by *name from a
plan*, not constructed by hand at a call site.  This module is that lookup,
and it is where the plan's mistakes are caught.

Three rules, each of which would otherwise be a silently wrong experiment:

* **A random strategy without a seed is refused.**  Design section 13 requires
  the batch plan to fix the seed and the run to record it; a strategy that
  defaulted to an unseeded RNG would produce a run nothing could reproduce,
  and the failure would only be visible when someone tried.
* **An LLM strategy without a transport is refused.**  There is no default
  model, and no environment lookup: a strategy that silently picked up a
  backend from an API key in the environment would make "which model was
  this?" a question about the machine rather than about the plan.
* **A non-LLM strategy given a transport is refused.**  This is the one that
  protects the comparison.  If ``DETERMINISTIC`` accepted a transport and
  quietly ignored it, a plan could believe it had configured a model arm while
  running an LLM-free one -- and the resulting table would be wrong in a way
  no assertion downstream could detect.

:func:`build_comparable_llm_pair` exists for the same reason.  Task section 11
requires the monolithic comparison to use "the same foundation model and a
controlled equivalent token/tool-call budget"; building the pair in one call
over one transport and one
:class:`~assurance.advisors.strategies.budget.AgentBudget` makes that true by
construction instead of by two call sites agreeing.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional, Tuple

from assurance.advisors.strategies.adversarial import AdversarialAdvisorStrategy
from assurance.advisors.strategies.basic import DeterministicStrategy, RandomStrategy
from assurance.advisors.strategies.budget import DEFAULT_COMPARABLE_BUDGET, AgentBudget
from assurance.advisors.strategies.llm_coordinator import RoleSeparatedLLMCoordinator
from assurance.advisors.strategies.monolithic import MonolithicLLMStrategy
from assurance.advisors.strategies.optimization import OptimizationStrategy
from assurance.advisors.strategies.transport import AdvisoryTransport
from assurance.advisors.strategy import AdvisoryStrategy, StrategyKind

__all__ = [
    "LLM_STRATEGY_KINDS",
    "STRATEGY_CLASSES",
    "StrategyConfigurationError",
    "build_comparable_llm_pair",
    "build_strategy",
]

#: The two of the six that call a model.  Everything else must be able to run
#: with no transport at all, which is what makes a hardware-free, network-free
#: comparison arm possible.
LLM_STRATEGY_KINDS: Tuple[StrategyKind, ...] = (
    StrategyKind.LLM_EVIDENCE_COORDINATOR,
    StrategyKind.MONOLITHIC_LLM,
)

#: Every strategy kind design section 12 names, and the class that implements
#: it.  Complete by construction: a test asserts the mapping covers
#: :class:`~assurance.advisors.strategy.StrategyKind` exactly, so adding a
#: seventh kind without an implementation fails loudly.
STRATEGY_CLASSES: Mapping[StrategyKind, type] = {
    StrategyKind.LLM_EVIDENCE_COORDINATOR: RoleSeparatedLLMCoordinator,
    StrategyKind.DETERMINISTIC: DeterministicStrategy,
    StrategyKind.RANDOM: RandomStrategy,
    StrategyKind.OPTIMIZATION: OptimizationStrategy,
    StrategyKind.MONOLITHIC_LLM: MonolithicLLMStrategy,
    StrategyKind.ADVERSARIAL_ADVISOR: AdversarialAdvisorStrategy,
}


class StrategyConfigurationError(ValueError):
    """The run configuration does not describe a runnable strategy."""


def build_strategy(
    kind: StrategyKind,
    *,
    seed: Optional[int] = None,
    transport: Optional[AdvisoryTransport] = None,
    budget: Optional[AgentBudget] = None,
    strategy_id: Optional[str] = None,
    **kwargs: Any,
) -> AdvisoryStrategy:
    """Construct one strategy from a plan's fields.

    Raises :class:`StrategyConfigurationError` rather than substituting a
    default for anything the plan should have said.
    """
    if not isinstance(kind, StrategyKind):
        raise StrategyConfigurationError(f"{kind!r} is not a StrategyKind")
    is_llm = kind in LLM_STRATEGY_KINDS

    if is_llm and transport is None:
        raise StrategyConfigurationError(
            f"{kind.value} needs a transport; there is no default model and no "
            "environment lookup, so the plan states which model ran"
        )
    if not is_llm and transport is not None:
        raise StrategyConfigurationError(
            f"{kind.value} makes no model call; a transport here would mean the "
            "plan believes it configured a model arm that never runs"
        )
    if not is_llm and budget is not None:
        raise StrategyConfigurationError(
            f"{kind.value} spends no tokens; a token budget here would be recorded "
            "in the run record and never enforced"
        )
    if kind is StrategyKind.RANDOM and seed is None:
        raise StrategyConfigurationError(
            "RANDOM needs an explicit seed: design section 13 requires the batch "
            "plan to fix it and the run to record it"
        )
    if kind is not StrategyKind.RANDOM and seed is not None:
        raise StrategyConfigurationError(
            f"{kind.value} draws no randomness; a seed here would be recorded as "
            "reproducibility the strategy does not actually depend on"
        )

    arguments: dict = dict(kwargs)
    if strategy_id is not None:
        arguments["strategy_id"] = strategy_id
    if kind is StrategyKind.RANDOM:
        arguments["seed"] = seed
    if is_llm:
        arguments["transport"] = transport
        arguments["budget"] = budget if budget is not None else DEFAULT_COMPARABLE_BUDGET

    factory: Callable[..., Any] = STRATEGY_CLASSES[kind]
    return factory(**arguments)


def build_comparable_llm_pair(
    *,
    transport: AdvisoryTransport,
    budget: AgentBudget = DEFAULT_COMPARABLE_BUDGET,
) -> Tuple[RoleSeparatedLLMCoordinator, MonolithicLLMStrategy]:
    """The proposed method and its monolithic comparison, budget-matched.

    Returns ``(proposed, monolithic)`` over the *same* transport -- task
    section 11's "same foundation model" -- and the *same*
    :class:`~assurance.advisors.strategies.budget.AgentBudget`, metered
    separately so neither arm can starve the other.
    """
    if transport is None:
        raise StrategyConfigurationError("a comparable pair needs one shared transport")
    proposed = RoleSeparatedLLMCoordinator(transport=transport, budget=budget)
    monolithic = MonolithicLLMStrategy(transport=transport, budget=budget)
    return proposed, monolithic
