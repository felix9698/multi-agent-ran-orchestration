"""Strategy equivalence: the Kernel does not know which strategy ran.

Authority: ``task_0820_unified_ota_assurance_system.md`` section 11 ("전략이
바뀌어도 Kernel, contracts, catalog, hardware state와 verdict/evidence
evaluator는 바뀌지 않는다" and "agent timeout, malformed output, hallucinated
candidate와 exhausted opportunity에서 deterministic fallback으로 유한 종료
한다"), design section 12.

This is the file that makes the comparison a controlled experiment.  Two
claims, and the second is the one that keeps the first from being vacuous:

* **Equivalence.**  The same deployment run behind four different strategies
  reduces to the same terminal state hash once advisory traffic is stripped.
  Design section 4.3: "The same accepted event stream and reducer version must
  reproduce the same ledger and terminal state hash without any LLM."  If a
  strategy could reach anything the others could not, this is where it would
  show.
* **Finite termination.**  A strategy that times out, returns malformed
  output, hallucinates a candidate or never advances still ends the case.  A
  system that were equivalent only when every strategy behaved would be
  measuring the plumbing on its good days.
"""

from __future__ import annotations

import time
import unittest
from typing import Any, Mapping, Optional, Sequence

from assurance.advisors.coordinator import StrategyBackedEvidenceCoordinator
from assurance.advisors.messages import AdvisoryMessage
from assurance.advisors.proposal_support import build_next_candidate_proposal
from assurance.advisors.strategies import (
    DeterministicStrategy,
    MonolithicLLMStrategy,
    OptimizationStrategy,
    RandomStrategy,
    RoleSeparatedLLMCoordinator,
)
from assurance.advisors.strategy import StrategyKind
from assurance.core.axes import EvidenceCellStatus, TrialOutcome
from assurance.core.states import TrialState
from assurance.kernel.reducer import KernelReducer, replay, terminal_state_hash

from tests.assurance.strategy_support import (
    AVAILABLE_IDS,
    UNKNOWN_ID,
    choice_reply,
    intent_reply,
    scripted,
    views,
    xapp_reply,
)
from tests.assurance.vertical_support import CELL_ID, START, VerticalFixture, timeseries

#: The candidate every strategy is steered to in the equivalence run.  Fixing
#: it is the point: two strategies that chose *different* candidates would
#: produce different reductions for an honest reason, and the claim under test
#: is about the machinery, not about which candidate is better.
FIRST_CANDIDATE = "candidate/000000"

#: Advisory records are what "agent 제거" removes; they reduce to nothing.
ADVISORY_EVENT_KINDS = frozenset({"AdvisoryAccepted", "AdvisoryRejected"})


def _strip_advisory(events):
    return [event for event in events if event.event_kind not in ADVISORY_EVENT_KINDS]


def _vertical_script(candidate_id: str = FIRST_CANDIDATE):
    """A three-call conversation naming the vertical deployment's own cell."""
    return [
        intent_reply(cells=(CELL_ID,), note="one open cell"),
        xapp_reply(applicable=(candidate_id,), inapplicable=(), needs=(CELL_ID,)),
        choice_reply(candidate_id=candidate_id, cells=(CELL_ID,)),
    ]


class _SlowStrategy:
    """Never answers in time.  Design section 15's "agent timeout"."""

    strategy_id = "strategy-slow"
    strategy_kind = StrategyKind.DETERMINISTIC

    def __init__(self, *, delay_seconds: float = 0.5) -> None:
        self._delay = delay_seconds

    def propose(self, **kwargs: Any) -> Optional[AdvisoryMessage]:
        time.sleep(self._delay)
        return None

    def describe(self) -> Mapping[str, Any]:
        return {"strategyId": self.strategy_id}


class _MalformedStrategy:
    """Answers with something that is not a proposal at all."""

    strategy_id = "strategy-malformed"
    strategy_kind = StrategyKind.DETERMINISTIC

    def propose(self, **kwargs: Any) -> Any:
        return {"candidateId": FIRST_CANDIDATE}

    def describe(self) -> Mapping[str, Any]:
        return {"strategyId": self.strategy_id}


class _HallucinatingStrategy:
    """Names a candidate that is in no catalog."""

    strategy_id = "strategy-hallucinating"
    strategy_kind = StrategyKind.DETERMINISTIC

    def propose(
        self, *, correlation_id: str, epoch_hash: str, now: str, **kwargs: Any
    ) -> AdvisoryMessage:
        return build_next_candidate_proposal(
            message_id=f"{self.strategy_id}/{correlation_id}",
            candidate_id=UNKNOWN_ID,
            rationale="hallucinated",
            correlation_id=correlation_id,
            epoch_hash=epoch_hash,
            now=now,
        )

    def describe(self) -> Mapping[str, Any]:
        return {"strategyId": self.strategy_id}


class _ExplodingStrategy:
    """Raises instead of answering."""

    strategy_id = "strategy-exploding"
    strategy_kind = StrategyKind.DETERMINISTIC

    def propose(self, **kwargs: Any) -> Optional[AdvisoryMessage]:
        raise RuntimeError("the model process died")

    def describe(self) -> Mapping[str, Any]:
        return {"strategyId": self.strategy_id}


class TheTerminalStateDoesNotDependOnTheStrategy(VerticalFixture, unittest.TestCase):
    """The same run, four ways, one reduction."""

    def _run_behind(self, strategy) -> tuple:
        path = self.build(
            strategy=strategy, collector=timeseries(start=START, value=4.0)
        )
        candidate_id, rejection = path.request_proposal()
        self.assertIsNone(rejection)
        self.assertEqual(candidate_id, FIRST_CANDIDATE)
        report = path.run_trial(candidate_id, cell_id=CELL_ID)
        path.terminate()
        events = list(self.store.iterate())
        reducer = KernelReducer()
        digest = terminal_state_hash(
            replay(reducer, _strip_advisory(events)),
            reducer_version=reducer.reducer_version,
        )
        return report, digest, events

    def _strategies(self):
        return {
            "DETERMINISTIC": DeterministicStrategy(),
            "OPTIMIZATION": OptimizationStrategy(),
            "LLM_EVIDENCE_COORDINATOR": RoleSeparatedLLMCoordinator(
                transport=scripted(_vertical_script())
            ),
            "MONOLITHIC_LLM": MonolithicLLMStrategy(
                transport=scripted(
                    [choice_reply(candidate_id=FIRST_CANDIDATE, cells=(CELL_ID,))]
                )
            ),
        }

    def test_every_strategy_reaches_the_same_settled_success(self) -> None:
        for name, strategy in self._strategies().items():
            with self.subTest(strategy=name):
                report, _, _ = self._run_behind(strategy)
                self.assertEqual(report.terminal_state, TrialState.SETTLED_SUCCESS)
                self.assertEqual(report.outcome, TrialOutcome.SUCCESS)

    def test_every_strategy_reduces_to_the_same_terminal_state_hash(self) -> None:
        digests = {}
        for name, strategy in self._strategies().items():
            _, digest, _ = self._run_behind(strategy)
            digests[name] = digest
        self.assertEqual(len(set(digests.values())), 1, digests)

    def test_the_verdict_and_the_evidence_are_identical(self) -> None:
        reductions = []
        for strategy in self._strategies().values():
            _, _, events = self._run_behind(strategy)
            state = replay(KernelReducer(), _strip_advisory(events))
            reductions.append(
                {
                    "cells": {
                        cell_id: cell["status"]
                        for cell_id, cell in state["evidenceCells"].items()
                    },
                    "outcomes": sorted(
                        trial["outcome"] for trial in state["trials"].values()
                    ),
                }
            )
        for other in reductions[1:]:
            self.assertEqual(other, reductions[0])
        self.assertEqual(
            reductions[0]["cells"][CELL_ID], EvidenceCellStatus.CLOSED_PASS.value
        )

    def test_the_catalog_is_identical_under_every_strategy(self) -> None:
        # Design section 6.3: agents cannot add, remove or mutate candidates.
        catalogs = []
        for strategy in self._strategies().values():
            self._run_behind(strategy)
            catalog = self.kernel.current_catalog()
            catalogs.append(
                (catalog.catalog_hash, catalog.cardinality,
                 tuple(item.candidate_id for item in catalog.candidates))
            )
        for other in catalogs[1:]:
            self.assertEqual(other, catalogs[0])

    def test_only_the_advisory_records_differ_between_strategies(self) -> None:
        # The raw streams *should* differ -- different message ids, different
        # rationales.  Stripping advisory records is what makes the rest
        # comparable, and this asserts the difference is confined there.
        streams = []
        for strategy in self._strategies().values():
            _, _, events = self._run_behind(strategy)
            streams.append(
                [
                    (event.event_kind, event.object_id)
                    for event in _strip_advisory(events)
                ]
            )
        for other in streams[1:]:
            self.assertEqual(other, streams[0])


class EveryFailureModeStillTerminates(unittest.TestCase):
    """Task section 11's four named failure modes, at the coordinator."""

    def _coordinator(self, strategy, *, timeout_seconds: float = 2.0):
        return StrategyBackedEvidenceCoordinator(
            strategy=strategy, timeout_seconds=timeout_seconds
        )

    def test_an_agent_timeout_ends_in_the_deterministic_fallback(self) -> None:
        coordinator = self._coordinator(_SlowStrategy(delay_seconds=0.5), timeout_seconds=0.05)
        started = time.monotonic()
        message = coordinator.propose_next(**views())
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.4, "the coordinator did not bound the strategy")
        self.assertIn(message.body.candidate_id, AVAILABLE_IDS)
        self.assertIn("timeout", coordinator.fallback_events[0].reason)

    def test_malformed_output_ends_in_the_deterministic_fallback(self) -> None:
        coordinator = self._coordinator(_MalformedStrategy())
        message = coordinator.propose_next(**views())
        self.assertIn(message.body.candidate_id, AVAILABLE_IDS)
        self.assertIn("malformed", coordinator.fallback_events[0].reason)

    def test_a_raising_strategy_ends_in_the_deterministic_fallback(self) -> None:
        coordinator = self._coordinator(_ExplodingStrategy())
        message = coordinator.propose_next(**views())
        self.assertIn(message.body.candidate_id, AVAILABLE_IDS)
        self.assertIn("exception", coordinator.fallback_events[0].reason)

    def test_a_hallucinated_candidate_is_not_repaired_by_the_coordinator(self) -> None:
        # The coordinator checks shape, not catalog membership -- it has no
        # catalog.  This is the division of labour, asserted so a future
        # "helpful" membership check in the coordinator does not silently
        # duplicate the Kernel's job in a place with less information.
        coordinator = self._coordinator(_HallucinatingStrategy())
        message = coordinator.propose_next(**views())
        self.assertEqual(message.body.candidate_id, UNKNOWN_ID)
        self.assertEqual(coordinator.fallback_events, ())

    def test_the_fallback_respects_the_same_budget_view(self) -> None:
        # A fallback with relaxed rules would be a way to keep a case alive
        # past its caps (design section 8).
        coordinator = self._coordinator(_ExplodingStrategy())
        exhausted = {"trials_used": 3, "max_trials": 3}
        self.assertIsNone(coordinator.propose_next(**views(budget_view=exhausted)))
        self.assertEqual(len(coordinator.fallback_events), 1)

    def test_repeated_failure_is_bounded_and_recorded_every_time(self) -> None:
        coordinator = self._coordinator(_ExplodingStrategy())
        for _ in range(5):
            coordinator.propose_next(**views())
        self.assertEqual(len(coordinator.fallback_events), 5)


class EveryStrategyIsBoundedByTheSameCoordinator(unittest.TestCase):
    """Substituting a strategy changes the proposal and nothing else."""

    def _all(self):
        return (
            DeterministicStrategy(),
            RandomStrategy(seed=3),
            OptimizationStrategy(),
            RoleSeparatedLLMCoordinator(transport=scripted(_vertical_script())),
            MonolithicLLMStrategy(transport=scripted([choice_reply()])),
        )

    def test_each_is_accepted_by_the_one_coordinator(self) -> None:
        for strategy in self._all():
            with self.subTest(strategy=strategy.strategy_id):
                coordinator = StrategyBackedEvidenceCoordinator(strategy=strategy)
                self.assertIs(coordinator.strategy, strategy)

    def test_none_of_them_causes_a_fallback_when_it_answers_honestly(self) -> None:
        for strategy in self._all():
            with self.subTest(strategy=strategy.strategy_id):
                coordinator = StrategyBackedEvidenceCoordinator(strategy=strategy)
                message = coordinator.propose_next(**views())
                self.assertIsNotNone(message)
                self.assertEqual(coordinator.fallback_events, ())

    def test_the_coordinator_cannot_have_its_strategy_swapped_mid_case(self) -> None:
        # A strategy change is a run-configuration decision (design section
        # 13), not something the coordinator should be asked to do mid-case.
        coordinator = StrategyBackedEvidenceCoordinator(strategy=DeterministicStrategy())
        self.assertFalse(
            any(
                name.startswith("set_") or name in {"swap", "replace"}
                for name in dir(coordinator)
                if not name.startswith("_")
            )
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
