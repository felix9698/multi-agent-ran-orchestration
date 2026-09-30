"""Gate 2 acceptance: catalog determinism, event replay, and the agent-free run.

Three of task section 13's Gate 2 criteria, in the order they appear:

* "catalog count/hash deterministic";
* "event replay final state hash 동일";
* "agent 제거 후 replay 결과 동일".

The third is the load-bearing one.  Design section 4.3: "The same accepted
event stream and reducer version must reproduce the same ledger and terminal
state hash without any LLM."  It is checked twice over, in the two ways it can
be false: by replaying a run's stream with the advisory records removed, and by
running the whole vertical path again with no coordinator constructed at all.
If an agent had contributed anything to the terminal state, exactly one of
those two would diverge.
"""

from __future__ import annotations

import unittest

from assurance.contracts.catalog import catalog_hash, generate_catalog
from assurance.core.axes import TrialOutcome
from assurance.kernel.reducer import KernelReducer, replay, terminal_state_hash

from tests.assurance.vertical_support import (
    ADAPTER,
    CELL_ID,
    START,
    VerticalFixture,
    contract_set,
    identity,
    timeseries,
)

#: Advisory traffic is retained in the stream and reduces to nothing; these
#: are the records "agent 제거" removes.
ADVISORY_EVENT_KINDS = frozenset({"AdvisoryAccepted", "AdvisoryRejected"})


def strip_advisory(events):
    return [event for event in events if event.event_kind not in ADVISORY_EVENT_KINDS]


class CatalogDeterminismTests(unittest.TestCase):
    """The candidate universe is the same object every time it is generated."""

    def test_generating_the_catalog_twice_gives_the_same_count_and_hash(self) -> None:
        arguments = dict(
            generator_version="assurance-catalog/1.0.0",
            epoch_ref="epoch/determinism",
            identity=identity("catalog/determinism"),
        )
        first_data = contract_set()
        second_data = contract_set()

        first = generate_catalog(
            targets=(first_data["target"],),
            capabilities=(first_data["capability"],),
            composition=first_data["composition"],
            **arguments,
        )
        second = generate_catalog(
            targets=(second_data["target"],),
            capabilities=(second_data["capability"],),
            composition=second_data["composition"],
            **arguments,
        )

        self.assertEqual(first.cardinality, 2)
        self.assertEqual(second.cardinality, first.cardinality)
        self.assertEqual(second.catalog_hash, first.catalog_hash)
        self.assertEqual(
            [candidate.candidate_id for candidate in second.candidates],
            [candidate.candidate_id for candidate in first.candidates],
        )
        self.assertEqual(
            [candidate.semantic_hash for candidate in second.candidates],
            [candidate.semantic_hash for candidate in first.candidates],
        )
        # The hash is recomputable from the recorded membership, which is what
        # lets the Kernel verify a catalog it did not generate.
        self.assertEqual(
            first.catalog_hash,
            catalog_hash(
                generator_version=first.generator_version,
                cardinality=first.cardinality,
                candidates=first.candidates,
            ),
        )


class EpochDeterminismTests(unittest.TestCase):
    """Two independent runs of the same admission produce the same epoch."""

    def test_two_independent_freezes_agree_on_epoch_and_catalog(self) -> None:
        class Fixture(VerticalFixture):
            pass

        first, second = Fixture(), Fixture()
        first.build()
        second.build()

        self.assertEqual(second.epoch.epoch_id, first.epoch.epoch_id)
        self.assertEqual(second.epoch.catalog_hash, first.epoch.catalog_hash)
        self.assertEqual(
            second.epoch.candidate_universe_cardinality,
            first.epoch.candidate_universe_cardinality,
        )
        self.assertEqual(
            second.epoch.candidate_semantic_hashes,
            first.epoch.candidate_semantic_hashes,
        )
        self.assertEqual(
            second.kernel.reduced_state()["epochs"][second.epoch.epoch_id]["epochHash"],
            first.kernel.reduced_state()["epochs"][first.epoch.epoch_id]["epochHash"],
        )


class ReplayTests(VerticalFixture, unittest.TestCase):
    """The stream is the system of record; replaying it is the check."""

    def run_success(self, *, with_coordinator: bool = True):
        path = self.build(with_coordinator=with_coordinator)
        candidate_id = (
            path.request_proposal()[0]
            if with_coordinator
            else self.first_candidate_id()
        )
        report = path.run_trial(candidate_id, cell_id=CELL_ID)
        self.assertEqual(report.outcome, TrialOutcome.SUCCESS)
        path.terminate()
        return path

    def test_replaying_the_stream_reproduces_the_terminal_state_hash(self) -> None:
        path = self.run_success()
        live = path.terminal_state_hash()

        reducer = KernelReducer()
        first = replay(reducer, list(self.store.iterate()))
        second = replay(KernelReducer(), list(self.store.iterate()))

        self.assertEqual(first, second)
        self.assertEqual(
            terminal_state_hash(first, reducer_version=reducer.reducer_version), live
        )
        self.assertEqual(
            terminal_state_hash(second, reducer_version=reducer.reducer_version), live
        )

    def test_removing_the_agent_records_does_not_change_the_terminal_state(self) -> None:
        path = self.run_success()
        reducer = KernelReducer()
        events = list(self.store.iterate())
        stripped = strip_advisory(events)

        self.assertLess(len(stripped), len(events))
        self.assertEqual(
            terminal_state_hash(
                replay(reducer, stripped), reducer_version=reducer.reducer_version
            ),
            path.terminal_state_hash(),
        )

    def test_a_run_with_no_coordinator_reaches_the_same_terminal_state(self) -> None:
        with_agent = self.run_success(with_coordinator=True)
        agent_hash = with_agent.terminal_state_hash()
        agent_events = len(list(self.store.iterate()))

        without = self.run_success(with_coordinator=False)

        self.assertIsNone(without.coordinator)
        self.assertLess(len(list(self.store.iterate())), agent_events)
        self.assertEqual(without.terminal_state_hash(), agent_hash)

    def test_the_rollback_path_replays_identically_too(self) -> None:
        path = self.build(collector=timeseries(start=START, value=1.5))
        report = path.run_trial(path.request_proposal()[0], cell_id=CELL_ID)
        self.assertEqual(report.outcome, TrialOutcome.FAIL)

        reducer = KernelReducer()
        events = list(self.store.iterate())
        self.assertEqual(
            terminal_state_hash(
                replay(reducer, events), reducer_version=reducer.reducer_version
            ),
            path.terminal_state_hash(),
        )
        self.assertEqual(
            terminal_state_hash(
                replay(KernelReducer(), strip_advisory(events)),
                reducer_version=reducer.reducer_version,
            ),
            path.terminal_state_hash(),
        )

    def test_the_reducer_version_is_part_of_what_the_hash_means(self) -> None:
        path = self.run_success()
        state = replay(KernelReducer(), list(self.store.iterate()))

        self.assertNotEqual(
            terminal_state_hash(state, reducer_version="assurance-kernel-reducer/0.0.0"),
            path.terminal_state_hash(),
        )

    def test_the_adapter_that_acted_is_the_one_the_plan_named(self) -> None:
        self.run_success()
        staged = [
            envelope.payload
            for envelope in self.store.iterate()
            if envelope.event_kind == "TrialPlanStaged"
        ]
        self.assertEqual([item["adapter"] for item in staged], [ADAPTER])
        self.assertEqual(
            sorted(self.gateway.registered_paths()), [ADAPTER]
        )


if __name__ == "__main__":
    unittest.main()
