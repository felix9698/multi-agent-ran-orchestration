"""The Agent's coordination layer, hermetically: board, priorities, search.

Every test here is about the mechanism being general in the number of intents
and actions.  The three-intent, three-action examples are examples; the same
code is exercised with two, four and five to show nothing is hard-wired.

The vocabulary is the owner's: a prime on either side means *deferred this
round* -- the intent is not held now, the action is not executed now -- and is
a priority judgement about this moment, never a failure class.
"""

from __future__ import annotations

import unittest
from typing import Dict, List, Mapping, Optional

from assurance.coordination import (
    ActionAxisView, ActionPriorityCoordinator, ActionState, ActionVerification, Board,
    BoardSearcher, CandidateView, IntentPriorityCoordinator, IntentState, IntentView,
    OutcomeKind, PairComparator, PairProvenance, PriorityPolicy, StatePair,
    TerminationReason, action_states_from_candidate, derive_state_pair,
    intent_states_from_verdicts,
)


def pair(candidate_id: str, intents: Mapping[str, str], actions: Mapping[str, str],
         *, parameters: Optional[Mapping[str, str]] = None,
         outcome: OutcomeKind = OutcomeKind.EVALUATED,
         provenance: PairProvenance = PairProvenance.OBSERVED,
         verification: Optional[Mapping[str, str]] = None) -> StatePair:
    return StatePair(
        candidate_id=candidate_id, parameters=parameters or {},
        intent_states={k: IntentState(v) for k, v in intents.items()},
        action_states={k: ActionState(v) for k, v in actions.items()},
        provenance=provenance, outcome_kind=outcome,
        action_verification={k: ActionVerification(v) for k, v in (verification or {}).items()})


P, D = "PRIORITIZED", "DEFERRED"


class TheBoardRecordsWholeStatePairs(unittest.TestCase):

    def test_the_owners_example_is_one_cell(self) -> None:
        """(I1', I2, I3) <-> (A1, A2', A3) is one recorded pair, primes and all."""
        cell = pair("candidate/000003", {"I1": D, "I2": P, "I3": P},
                    {"A1": P, "A2": D, "A3": P})
        board = Board()
        board.record(cell)
        self.assertEqual(("I1'", "I2", "I3"), cell.intent_tuple(("I1", "I2", "I3")))
        self.assertEqual(("A1", "A2'", "A3"), cell.action_tuple(("A1", "A2", "A3")))
        self.assertEqual("(I1', I2, I3) <-> (A1, A2', A3)",
                         cell.label(("I1", "I2", "I3"), ("A1", "A2", "A3")))
        self.assertFalse(cell.all_intents_prioritized)
        self.assertEqual(("I1",), cell.deferred_intents())
        self.assertEqual(("A2",), cell.deferred_actions())
        self.assertEqual(1, len(board))

    def test_the_tuple_length_follows_the_intent_and_action_sets(self) -> None:
        for n, m in ((2, 1), (4, 3), (5, 6)):
            with self.subTest(intents=n, actions=m):
                intents = {f"I{k}": P for k in range(1, n + 1)}
                actions = {f"A{k}": P for k in range(1, m + 1)}
                cell = pair("c", intents, actions)
                self.assertEqual(n, len(cell.intent_tuple(tuple(intents))))
                self.assertEqual(m, len(cell.action_tuple(tuple(actions))))

    def test_a_prime_is_a_record_and_nothing_else(self) -> None:
        cell = pair("c", {"I1": D}, {"A1": D})
        record = cell.to_record()
        self.assertEqual({"candidateId", "parameters", "intentStates", "actionStates",
                          "provenance", "outcomeKind", "conditions", "trialId",
                          "recordedAt", "detail", "actionVerification"}, set(record))
        self.assertEqual(cell, StatePair.from_record(record))

    def test_verification_sits_beside_the_pair_not_in_it(self) -> None:
        """An executed action is A whether or not its write read back."""
        verified = pair("v", {"I1": P}, {"A1": P}, verification={"A1": "VERIFIED"})
        unverified = pair("u", {"I1": P}, {"A1": P}, verification={"A1": "UNVERIFIED"})
        self.assertEqual(verified.action_tuple(("A1",)), unverified.action_tuple(("A1",)))
        self.assertTrue(verified.executed_actions_verified)
        self.assertFalse(unverified.executed_actions_verified)

    def test_observed_and_predicted_are_kept_apart(self) -> None:
        board = Board()
        board.record(pair("c1", {"I1": P}, {"A1": P}))
        board.record(pair("c2", {"I1": P}, {"A1": P}, provenance=PairProvenance.PREDICTED))
        self.assertEqual(("c1",), tuple(p.candidate_id for p in board.observed()))
        self.assertEqual(("c2",), tuple(p.candidate_id for p in board.predicted()))
        self.assertEqual(("c1",), board.tried())

    def test_an_execution_error_is_uninformative_not_a_deferred_intent(self) -> None:
        cell = pair("c", {"I1": "UNKNOWN"}, {"A1": P}, outcome=OutcomeKind.EXEC_ERROR)
        self.assertFalse(cell.informative)
        self.assertNotIn(cell, Board([cell]).informative_observed())


class DerivationReadsTheKernelsOwnRecords(unittest.TestCase):

    def test_intent_states_come_from_per_predicate_verdicts(self) -> None:
        states = intent_states_from_verdicts(
            {"I1": ("I1/min", "I1/max"), "I2": ("I2/floor",), "I3": ("I3/p",)},
            {"I1/min": "PASS", "I1/max": "FAIL", "I2/floor": "PASS"})
        self.assertEqual({"I1": IntentState.DEFERRED, "I2": IntentState.PRIORITIZED,
                          "I3": IntentState.UNKNOWN}, states)

    def test_action_states_come_from_which_axes_the_candidate_moved(self) -> None:
        states = action_states_from_candidate(
            {"servingCell@22": "87654321", "dlPrbCap@24": "0"},
            {"steer@22": "servingCell@22", "cap@24": "dlPrbCap@24"},
            {"servingCell@22": "12345678", "dlPrbCap@24": "0"})
        self.assertEqual({"steer@22": ActionState.PRIORITIZED, "cap@24": ActionState.DEFERRED},
                         states)

    def test_derive_keeps_verification_beside_the_states(self) -> None:
        cell = derive_state_pair(
            candidate_id="candidate/000001",
            parameters={"servingCell@22": "87654321", "dlPrbCap@24": "0"},
            intent_predicates={"I1": ("I1/min",), "I2": ("I2/min",)},
            evaluation={"executionValidity": "VALID",
                        "predicateVerdicts": {"I1/min": "FAIL", "I2/min": "PASS"}},
            trial_outcome="FAIL",
            action_axes={"steer@22": "servingCell@22", "cap@24": "dlPrbCap@24"},
            baselines={"servingCell@22": "12345678", "dlPrbCap@24": "0"},
            axis_verification={"servingCell@22": ActionVerification.UNVERIFIED})
        self.assertEqual(IntentState.DEFERRED, cell.intent_states["I1"])
        self.assertEqual(ActionState.PRIORITIZED, cell.action_states["steer@22"])
        self.assertEqual(ActionState.DEFERRED, cell.action_states["cap@24"])
        self.assertEqual(ActionVerification.UNVERIFIED, cell.action_verification["steer@22"])
        self.assertEqual(ActionVerification.NOT_EXECUTED, cell.action_verification["cap@24"])
        self.assertIs(OutcomeKind.EVALUATED, cell.outcome_kind)
        self.assertEqual("(I1', I2) <-> (steer@22, cap@24')",
                         cell.label(("I1", "I2"), ("steer@22", "cap@24")))

    def test_an_unevaluated_trial_leaves_every_intent_unknown(self) -> None:
        cell = derive_state_pair(
            candidate_id="c", parameters={"x": "1"}, intent_predicates={"I1": ("p",), "I2": ("q",)},
            evaluation={"executionValidity": "NOT_EVALUATED", "predicateVerdicts": {}},
            trial_outcome="EXEC_ERROR", action_axes={"a": "x"}, baselines={"x": "0"},
            axis_verification={"x": ActionVerification.REFUSED})
        self.assertEqual({"I1": IntentState.UNKNOWN, "I2": IntentState.UNKNOWN},
                         cell.intent_states)
        self.assertIs(OutcomeKind.EXEC_ERROR, cell.outcome_kind)


class PrioritiesChangeTheComparison(unittest.TestCase):

    def setUp(self) -> None:
        self.a = pair("a", {"I1": P, "I2": D}, {"A1": P})
        self.b = pair("b", {"I1": D, "I2": P}, {"A1": P})

    def comparator(self, order):
        return PairComparator(IntentPriorityCoordinator(order),
                              ActionPriorityCoordinator(("A1",)))

    def test_lexicographic_protection_flips_with_the_stated_order(self) -> None:
        self.assertTrue(self.comparator(("I1", "I2")).better(self.a, self.b))
        self.assertTrue(self.comparator(("I2", "I1")).better(self.b, self.a))

    def test_weighted_policy_uses_the_weights(self) -> None:
        heavy_i2 = PairComparator(
            IntentPriorityCoordinator(("I1", "I2"), policy=PriorityPolicy.WEIGHTED,
                                      weights={"I1": 1.0, "I2": 5.0}),
            ActionPriorityCoordinator(("A1",)))
        self.assertTrue(heavy_i2.better(self.b, self.a))

    def test_action_priority_only_breaks_intent_ties(self) -> None:
        cmp = PairComparator(IntentPriorityCoordinator(("I1",)),
                             ActionPriorityCoordinator(("A1", "A2")))
        x = pair("x", {"I1": P}, {"A1": P, "A2": D})
        y = pair("y", {"I1": P}, {"A1": D, "A2": P})
        self.assertTrue(cmp.better(x, y), "deferring A2 is preferred to deferring A1")
        z = pair("z", {"I1": D}, {"A1": P, "A2": P})
        self.assertTrue(cmp.better(y, z), "a deferred intent is never outranked by actions")

    def test_uninformative_pairs_rank_below_informative_ones(self) -> None:
        cmp = self.comparator(("I1", "I2"))
        error = pair("e", {"I1": "UNKNOWN", "I2": "UNKNOWN"}, {"A1": P},
                     outcome=OutcomeKind.EXEC_ERROR)
        self.assertTrue(cmp.better(self.b, error))

    def test_unstated_intents_rank_after_stated_ones_in_admission_order(self) -> None:
        coordinator = IntentPriorityCoordinator(("I3",))
        self.assertEqual(("I3", "I1", "I2"), coordinator.ranking(("I1", "I2", "I3")))

    def test_the_action_role_says_which_action_is_deferred_first(self) -> None:
        coordinator = ActionPriorityCoordinator(("steer@22", "steer@24", "cap@24"))
        self.assertEqual(("cap@24", "steer@24", "steer@22"),
                         coordinator.most_deferrable_first(("steer@22", "steer@24", "cap@24")))


def _catalog(values: Mapping[str, tuple]) -> List[CandidateView]:
    """Every combination of the axes, in a deterministic order, like the Kernel."""
    import itertools

    keys = sorted(values)
    out = []
    for index, combination in enumerate(itertools.product(*(values[k] for k in keys))):
        out.append(CandidateView(f"candidate/{index:06d}", dict(zip(keys, combination))))
    return out


class TheSearchUsesTheBoardAndThePriorities(unittest.TestCase):

    def setUp(self) -> None:
        self.catalog = _catalog({"servingCell@22": ("12345678", "87654321"),
                                 "servingCell@24": ("12345678", "87654321"),
                                 "dlPrbCap@24": ("0", "12")})
        self.intents = (IntentView("I1", "22"), IntentView("I2", "24"))
        self.actions = (
            ActionAxisView("steer@22", "servingCell@22", "22", "12345678"),
            ActionAxisView("steer@24", "servingCell@24", "24", "12345678"),
            ActionAxisView("cap@24", "dlPrbCap@24", "24", "0", shared=True),
        )
        self.comparator = PairComparator(
            IntentPriorityCoordinator(("I1", "I2")),
            ActionPriorityCoordinator(("steer@22", "steer@24", "cap@24")))

    def searcher(self, **kwargs) -> BoardSearcher:
        kwargs.setdefault("budget_trials", 4)
        kwargs.setdefault("comparator", self.comparator)
        return BoardSearcher(catalog=self.catalog, intents=self.intents,
                             actions=self.actions, **kwargs)

    def _by_params(self, **params) -> CandidateView:
        wanted = {k: str(v) for k, v in params.items()}
        return next(item for item in self.catalog if item.parameters == wanted)

    def _baseline(self) -> CandidateView:
        return self._by_params(**{"servingCell@22": "12345678",
                                  "servingCell@24": "12345678", "dlPrbCap@24": "0"})

    def _all_deferred(self) -> Dict[str, str]:
        return {"steer@22": D, "steer@24": D, "cap@24": D}

    def test_first_choice_is_the_candidate_closest_to_the_baseline(self) -> None:
        decision = self.searcher().next(Board(), trials_used=0)
        self.assertEqual(self._baseline().candidate_id, decision.candidate_id)
        self.assertIsNone(decision.termination)
        self.assertIn("closest to the baseline", decision.rationale)

    def test_the_next_move_targets_the_highest_priority_deferred_intent(self) -> None:
        baseline = self._baseline()
        board = Board([pair(baseline.candidate_id, {"I1": D, "I2": P}, self._all_deferred(),
                            parameters=baseline.parameters)])
        decision = self.searcher().next(board, trials_used=1)
        chosen = next(item for item in self.catalog
                      if item.candidate_id == decision.candidate_id)
        changed = [axis for axis, value in chosen.parameters.items()
                   if value != baseline.parameters[axis]]
        self.assertEqual(1, len(changed))
        self.assertEqual("dlPrbCap@24", changed[0],
                         "cap@24 is the most deferrable action and can affect I1's cell")
        self.assertIn("I1", decision.rationale)

    def test_flipping_the_intent_priority_changes_the_next_move(self) -> None:
        baseline = self._baseline()
        board = Board([pair(baseline.candidate_id, {"I1": D, "I2": D}, self._all_deferred(),
                            parameters=baseline.parameters)])
        protect_i2 = PairComparator(
            IntentPriorityCoordinator(("I2", "I1")),
            ActionPriorityCoordinator(("cap@24", "steer@22", "steer@24")))
        first = self.searcher().next(board, trials_used=1)
        second = self.searcher(comparator=protect_i2).next(board, trials_used=1)
        self.assertNotEqual(first.candidate_id, second.candidate_id)
        self.assertIn("I2", second.rationale)

    def test_found_stops_the_search_whatever_actions_were_deferred(self) -> None:
        winner = self.catalog[3]
        board = Board([pair(winner.candidate_id, {"I1": P, "I2": P},
                            {"steer@22": P, "steer@24": D, "cap@24": P},
                            parameters=winner.parameters)])
        decision = self.searcher().next(board, trials_used=1)
        self.assertIs(TerminationReason.FOUND, decision.termination)
        self.assertEqual(winner.candidate_id, decision.candidate_id)

    def test_budget_exhausted_is_not_impossibility(self) -> None:
        board = Board([pair(self.catalog[0].candidate_id, {"I1": D, "I2": P},
                            {"steer@22": P}, parameters=self.catalog[0].parameters)])
        decision = self.searcher(budget_trials=1).next(board, trials_used=1)
        self.assertIs(TerminationReason.BUDGET_EXHAUSTED, decision.termination)
        self.assertIn("not a proof", decision.rationale)
        self.assertIsNotNone(decision.best_so_far)

    def test_catalog_exhausted_only_over_the_frozen_catalog(self) -> None:
        board = Board([pair(item.candidate_id, {"I1": D, "I2": P}, {"steer@22": P},
                            parameters=item.parameters)
                       for item in self.catalog])
        decision = self.searcher(budget_trials=100).next(board, trials_used=len(self.catalog))
        self.assertIs(TerminationReason.CATALOG_EXHAUSTED, decision.termination)
        self.assertIn("this catalog only", decision.rationale)

    def test_undecidable_is_distinguished_from_deferred(self) -> None:
        board = Board([
            pair(self.catalog[0].candidate_id, {"I1": "UNKNOWN", "I2": "UNKNOWN"},
                 {"steer@22": P}, outcome=OutcomeKind.EXEC_ERROR),
            pair(self.catalog[1].candidate_id, {"I1": "UNKNOWN", "I2": "UNKNOWN"},
                 {"steer@22": P}, outcome=OutcomeKind.INDETERMINATE),
        ])
        decision = self.searcher(budget_trials=10).next(board, trials_used=2)
        self.assertIs(TerminationReason.UNDECIDABLE, decision.termination)
        self.assertIn("not the combinations", decision.rationale)

    def test_a_tried_candidate_is_not_retried_but_others_remain(self) -> None:
        tried = self.catalog[0]
        board = Board([pair(tried.candidate_id, {"I1": D, "I2": D}, {"steer@22": P},
                            parameters=tried.parameters)])
        decision = self.searcher(budget_trials=10).next(board, trials_used=1)
        self.assertNotEqual(tried.candidate_id, decision.candidate_id)
        self.assertNotIn(tried.candidate_id, decision.ranked)
        self.assertEqual(len(self.catalog) - 1, len(decision.ranked))

    def test_legality_prunes_candidates_without_marking_them_impossible(self) -> None:
        illegal = {item.candidate_id for item in self.catalog
                   if item.parameters["dlPrbCap@24"] != "0"}
        decision = self.searcher(legality=lambda p: p["dlPrbCap@24"] == "0").next(
            Board(), trials_used=0)
        self.assertTrue(set(decision.ranked).isdisjoint(illegal))

    def test_predictions_rank_but_stay_predicted(self) -> None:
        winner = self.catalog[5]

        def predictor(parameters):
            item = next(c for c in self.catalog if c.parameters == parameters)
            held = item.candidate_id == winner.candidate_id
            return pair(item.candidate_id, {"I1": P if held else D, "I2": P},
                        {"steer@22": P}, parameters=item.parameters,
                        provenance=PairProvenance.PREDICTED)

        board = Board()
        decision = self.searcher(predictor=predictor).next(board, trials_used=0)
        self.assertEqual(winner.candidate_id, decision.candidate_id)
        self.assertEqual(len(self.catalog), len(board.predicted()))
        self.assertEqual((), board.tried(), "a prediction is never a trial")
        self.assertIn("PREDICTED", decision.rationale)

    def test_the_searcher_record_names_its_rules_and_decisions(self) -> None:
        searcher = self.searcher()
        searcher.next(Board(), trials_used=0)
        record = searcher.to_record()
        self.assertEqual(4, record["budgetTrials"])
        self.assertEqual("intent-priority", record["comparator"]["intents"]["role"])
        self.assertEqual(1, len(record["decisions"]))
        self.assertEqual(3, len(record["actions"]))


if __name__ == "__main__":
    unittest.main()
