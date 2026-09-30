"""``T`` = the mandatory targets plus the Target agent's additions (v3.1).

``OTA_IMPLEMENTATION_AMENDMENT_20260914`` section 3 replaced "the original plus
at most nine, allocated 4/3/2": code always includes the original and every
maximally weakened authorized target, and the model adds up to six distinct
authorized vectors beyond them.  The agent still names only level indices --
every threshold, concession and ranking is computed from the authorization, so
it picks directions and never a value.

What changed from the old rule, and is pinned here because each one used to be
pinned the other way:

* an explicit empty list gives the mandatory set, not ``T0`` alone;
* an unauthorized, level-less or non-integer row is **refused** (the counted
  revision path), not dropped with a note;
* more than six genuine additions is refused, not cut -- no silent truncation;
* duplicates and restated mandatory targets are removed with a provenance log
  and never spend an addition;
* mandatory and model-authored membership are stored separately, and the exact
  expression error epsilon is recorded on every ``T``.

Hermetic: no model, no radio.
"""
import json
import unittest
from pathlib import Path

from assurance.coordination.tc import (
    MAX_MODEL_ADDITIONS, MAX_SELECTED_ALTERNATIVES, MAX_TARGETS, Authorization,
    Intent, Requirement, TargetValidationError, _axis_levels, boundary_targets,
    expand_targets, expression_error, mandatory_contract, preference_key,
    validate_target_contract)

PILOT = (Path(__file__).resolve().parents[1] / 'experiment_results' / 'ota-20260911'
         / 'pilot38-v3-existing3-20260914T2300')


def authorization():
    """Three relaxable requirements, no mode table: 27 targets, one boundary [2,2,2]."""
    intents = [
        Intent(intent_id='I1', owner='ue1-video', priority=1, weight=3.0, ue_id='425',
               requirement=Requirement(req_id='I1.r1', kpi='dlGoodputMbps', op='>=',
                                       value=0.8, unit='Mbps', scope='ue@425',
                                       bound=0.4, steps=2, relaxable=True)),
        Intent(intent_id='I2', owner='ue2-map', priority=2, weight=2.0, ue_id='426',
               requirement=Requirement(req_id='I2.r1', kpi='dlGoodputMbps', op='>=',
                                       value=0.15, unit='Mbps', scope='ue@426',
                                       bound=0.08, steps=2, relaxable=True)),
        Intent(intent_id='I3', owner='ue3-incumbent', priority=3, weight=1.0, ue_id='424',
               requirement=Requirement(req_id='I3.r1', kpi='dlGoodputMbps', op='>=',
                                       value=0.15, unit='Mbps', scope='ue@424',
                                       bound=0.08, steps=2, relaxable=True)),
    ]
    return Authorization.from_intents(intents)


def answer(alternatives=None):
    auth = authorization()
    document = {
        't0': {'targetId': 'T0',
               'requirements': {req: auth.requirements[req].original
                                for req in auth.req_ids}},
        'levels': {req: {'steps': auth.requirements[req].steps,
                         'bound': auth.requirements[req].limit}
                   for req in auth.req_ids},
        'constraints': [], 'ranking': {},
    }
    if alternatives is not None:
        document['alternatives'] = alternatives
    return document, auth


BOUNDARY = {'I1.r1': 2, 'I2.r1': 2, 'I3.r1': 2}


def levels_of(target):
    return {req: int(q) for req, q in dict(target.levels).items()}


def additions_of(contract):
    return [row['levels'] for row
            in dict(contract.provenance)['targetMembership']['modelAdditions']]


class TheMandatoryTargetsAreDerivedNotListed(unittest.TestCase):
    def test_the_fixture_has_one_boundary_the_fully_relaxed_target(self):
        contract = expand_targets(authorization())
        self.assertEqual([BOUNDARY], [levels_of(t) for t in boundary_targets(contract)])

    @unittest.skipUnless((PILOT / 'intents.json').is_file(), 'the v3 pilot is not in this checkout')
    def test_the_v3_authority_yields_exactly_the_amendments_three_branch_ends(self):
        _contract, vectors = v3_omega()
        derived = sorted(vectors[id(t)] for t in boundary_targets(_contract))
        self.assertEqual(sorted([(2, 2, 2, 0, 0, 0), (2, 0, 2, 0, 1, 0), (2, 2, 1, 0, 0, 1)]),
                         derived)

    def test_the_limits_are_the_amendments(self):
        self.assertEqual((10, 6, 9), (MAX_TARGETS, MAX_MODEL_ADDITIONS,
                                      MAX_SELECTED_ALTERNATIVES))


def v3_omega():
    """The frozen v3 authority, read through the same intake the runner uses."""
    import main
    from assurance.coordination.intake import merge_answers
    rows = json.loads((PILOT / 'intents.json').read_text())['intents']
    intents = tuple(Intent.from_record(row) for row in rows)
    _merged, auth, _ = merge_answers(intents, Authorization.from_intents(intents),
                                     main._parse_answers(str(PILOT / 'answers.json')), {})
    contract = expand_targets(auth)
    axes = ['I1g.r1', 'I2g.r1', 'I3g.r1', 'I1d.r1#deadline', 'I2d.r1#deadline', 'I3d.r1#deadline']
    vectors = {}
    for target in (contract.t0,) + tuple(contract.alternatives):
        found = _axis_levels(target, auth)
        vectors[id(target)] = tuple(int(found.get(axis, 0)) for axis in axes)
    return contract, vectors


@unittest.skipUnless((PILOT / 'intents.json').is_file(), 'the v3 pilot is not in this checkout')
class TheExpressionErrorMatchesTheReview(unittest.TestCase):
    """Reference values from OTA_EQUATION_COVERAGE_REVIEW_20260914 section 4."""

    @classmethod
    def setUpClass(cls):
        cls.contract, vectors = v3_omega()
        cls.by_vector = {vector: target for target in
                         (cls.contract.t0,) + tuple(cls.contract.alternatives)
                         for tid, vector in vectors.items() if tid == id(target)}
        cls.omega = (cls.contract.t0,) + tuple(cls.contract.alternatives)
        cls.auth = cls.contract.authorization

    def epsilon(self, vectors):
        return expression_error([self.by_vector[v] for v in vectors], self.omega, self.auth)

    MANDATORY = [(0, 0, 0, 0, 0, 0), (2, 2, 2, 0, 0, 0), (2, 0, 2, 0, 1, 0), (2, 2, 1, 0, 0, 1)]

    def test_omega_has_54_members(self):
        self.assertEqual(54, len(self.omega))

    def test_the_four_mandatory_targets_alone_give_one(self):
        self.assertEqual(1.0, self.epsilon(self.MANDATORY))

    def test_the_reviews_seven_target_example_gives_one_half(self):
        seven = [(0, 0, 0, 0, 0, 0), (1, 2, 2, 0, 0, 0), (1, 0, 2, 0, 1, 0), (1, 2, 1, 0, 0, 1),
                 (2, 2, 2, 0, 0, 0), (2, 0, 2, 0, 1, 0), (2, 2, 1, 0, 0, 1)]
        self.assertEqual(0.5, self.epsilon(seven))

    def test_all_of_omega_gives_zero(self):
        self.assertEqual(0.0, self.epsilon(list(self.by_vector)))

    def test_a_missing_boundary_is_infinite_not_a_large_number(self):
        self.assertIsNone(self.epsilon(self.MANDATORY[:3]))

    def test_the_mandatory_contract_records_it(self):
        recorded = dict(mandatory_contract(self.contract).provenance)['expressionError']
        self.assertEqual((1.0, False, [], 54, 4),
                         (recorded['epsilon'], recorded['epsilonInfinite'],
                          recorded['missingMandatory'], recorded['omegaSize'],
                          recorded['targetCount']))


class WhatTheAgentCarries(unittest.TestCase):
    def test_an_absent_selection_carries_the_mandatory_targets(self):
        # At this layer an absent list is the same as an empty one; the agent
        # layer refuses a missing list as malformed before it gets here.
        document, auth = answer()
        contract, _notes = validate_target_contract(document, auth)
        self.assertEqual([BOUNDARY], [levels_of(t) for t in contract.alternatives])

    def test_an_explicit_empty_selection_carries_the_mandatory_targets(self):
        document, auth = answer([])
        contract, notes = validate_target_contract(document, auth)
        self.assertEqual([BOUNDARY], [levels_of(t) for t in contract.alternatives])
        self.assertEqual([], additions_of(contract), 'none of it is credited to the model')
        self.assertTrue(any('no additional target' in note for note in notes), notes)

    def test_only_the_chosen_additions_join_the_mandatory_targets(self):
        chosen = [{'levels': {'I1.r1': 0, 'I2.r1': 0, 'I3.r1': 1}},
                  {'levels': {'I1.r1': 1, 'I2.r1': 0, 'I3.r1': 0}}]
        document, auth = answer(chosen)
        contract, _ = validate_target_contract(document, auth)
        self.assertEqual(sorted([c['levels'] for c in chosen], key=str),
                         sorted(additions_of(contract), key=str))
        self.assertEqual(3, len(contract.alternatives), 'two additions and the one boundary')
        self.assertIn(BOUNDARY, [levels_of(t) for t in contract.alternatives])

    def test_T_keeps_the_owners_order_and_fresh_ids(self):
        document, auth = answer([{'levels': {'I1.r1': 2, 'I2.r1': 1, 'I3.r1': 2}},
                                 {'levels': {'I1.r1': 0, 'I2.r1': 0, 'I3.r1': 1}}])
        contract, _ = validate_target_contract(document, auth)
        self.assertEqual(['T1', 'T2', 'T3'], [t.target_id for t in contract.alternatives])
        keys = [preference_key(t, auth) for t in contract.alternatives]
        self.assertEqual(keys, sorted(keys))

    def test_thresholds_come_from_the_authorization_not_the_answer(self):
        document, auth = answer([{'levels': {'I1.r1': 1, 'I2.r1': 0, 'I3.r1': 0},
                                  'requirements': {'I1.r1': 0.01}}])
        contract, _ = validate_target_contract(document, auth)
        carried = next(t for t in contract.alternatives if levels_of(t)['I1.r1'] == 1)
        self.assertAlmostEqual(0.6, carried.requirements['I1.r1'])

    def test_the_expression_error_is_recorded_on_every_T(self):
        document, auth = answer([{'levels': {'I1.r1': 1, 'I2.r1': 1, 'I3.r1': 1}}])
        contract, _ = validate_target_contract(document, auth)
        recorded = dict(contract.provenance)['expressionError']
        self.assertFalse(recorded['epsilonInfinite'])
        self.assertEqual([], recorded['missingMandatory'])
        self.assertEqual(27, recorded['omegaSize'])


class WhatTheAgentCannotDo(unittest.TestCase):
    """Refusals go down the counted revision path; nothing is cut or dropped."""

    def refused(self, alternatives):
        document, auth = answer(alternatives)
        with self.assertRaises(TargetValidationError) as caught:
            validate_target_contract(document, auth)
        return str(caught.exception)

    def test_levels_the_authorization_does_not_admit_are_refused(self):
        message = self.refused([{'levels': {'I1.r1': 9, 'I2.r1': 0, 'I3.r1': 0}}])
        self.assertIn('outside the owner-authorized targets', message)

    def test_a_row_without_levels_is_refused(self):
        self.assertIn('names no levels', self.refused([{'targetId': 'T1'}]))

    def test_a_non_integer_level_is_refused(self):
        self.assertIn('not a whole number',
                      self.refused([{'levels': {'I1.r1': 'x', 'I2.r1': 0, 'I3.r1': 0}}]))

    def test_one_bad_row_refuses_the_answer_even_beside_good_ones(self):
        self.refused([{'levels': {'I1.r1': 1, 'I2.r1': 0, 'I3.r1': 0}},
                      {'levels': {'I1.r1': 9, 'I2.r1': 0, 'I3.r1': 0}}])

    def _distinct_additions(self, count):
        rows = []
        for a in range(3):
            for b in range(3):
                for c in range(3):
                    vector = {'I1.r1': a, 'I2.r1': b, 'I3.r1': c}
                    if vector in ({'I1.r1': 0, 'I2.r1': 0, 'I3.r1': 0}, BOUNDARY):
                        continue
                    rows.append({'levels': vector})
        return rows[:count]

    def test_exactly_six_additions_are_carried(self):
        document, auth = answer(self._distinct_additions(MAX_MODEL_ADDITIONS))
        contract, _ = validate_target_contract(document, auth)
        self.assertEqual(MAX_MODEL_ADDITIONS, len(additions_of(contract)))
        self.assertEqual(1 + 1 + MAX_MODEL_ADDITIONS, len(contract.targets))

    def test_nine_distinct_additions_are_refused_not_truncated(self):
        message = self.refused(self._distinct_additions(MAX_MODEL_ADDITIONS + 1))
        # 2026-09-22 오너 지시로 여덟 -> 여섯.  검증기가 실제로 허용하는 값과
        # 프롬프트가 말하는 값을 맞춘 것이므로 기대값도 상수에서 읽는다.
        self.assertIn('at most %d' % MAX_MODEL_ADDITIONS, message)

    def test_duplicates_and_restated_mandatory_targets_do_not_spend_an_addition(self):
        six = self._distinct_additions(MAX_MODEL_ADDITIONS)
        noise = [dict(six[0]), {'levels': dict(BOUNDARY)},
                 {'levels': {'I1.r1': 0, 'I2.r1': 0, 'I3.r1': 0}}]
        document, auth = answer(six + noise)
        contract, notes = validate_target_contract(document, auth)
        removed = dict(contract.provenance)['modelSelection']['removed']
        self.assertEqual(MAX_MODEL_ADDITIONS, len(additions_of(contract)))
        self.assertEqual(1, len(removed['duplicates']))
        self.assertEqual(2, len(removed['restatedMandatory']))
        self.assertTrue(any('removed before the addition limit' in note for note in notes))


class WhatTheContractSaysAboutIt(unittest.TestCase):
    def test_both_schemas_ask_for_additions_with_a_reason_and_evidence(self):
        from assurance.coordination.agents import _MONOLITH_FORM_SCHEMA, _TARGET_SCHEMA
        row = _TARGET_SCHEMA['alternatives'][0]
        self.assertIn('reason', row)
        self.assertIn('evidenceRefs', row)
        self.assertNotIn('selectionRole', row, 'the 4/3/2 categories are superseded')
        self.assertIs(_TARGET_SCHEMA['alternatives'], _MONOLITH_FORM_SCHEMA['alternatives'])

    def test_both_formation_prompts_ask_for_zero_to_eight_additions(self):
        from assurance.coordination.agents import (MONOLITH_FORM_SYSTEM_PROMPT,
                                                   TARGET_SYSTEM_PROMPT)
        for prompt in (TARGET_SYSTEM_PROMPT, MONOLITH_FORM_SYSTEM_PROMPT):
            self.assertIn('up to six additional distinct authorized level vectors, excluding the anchors, and aim to fill that limit', prompt)
            self.assertIn('input.mandatory_targets', prompt)
            # 2026-09-17 오너 고정 프롬프트: 빈 목록은 'Include alternatives even when empty' 로 적혔다.
            self.assertIn('Include alternatives even when empty', prompt)
            self.assertNotIn('at most nine', prompt)
            self.assertNotIn('up to four preferred repairs', prompt)

    def test_the_target_input_carries_the_mandatory_targets(self):
        from assurance.coordination.agents import TargetInputs
        auth = authorization()
        rows = TargetInputs(intents=(), authorization=auth).payload()['input.mandatory_targets']
        self.assertEqual([{'I1.r1': 0, 'I2.r1': 0, 'I3.r1': 0}, BOUNDARY],
                         [{req: int(q) for req, q in row['levels'].items()} for row in rows])


class TheDeadlineAxisIsPartOfTheIdentity(unittest.TestCase):
    """Two targets can share every threshold level and differ only in whether they
    also extend a measurement window. The cheaper of the pair leaves it alone, so a
    lookup keyed on thresholds alone resolved every choice to the dearer twin."""

    def authorization_with_a_deadline(self):
        intents = [
            Intent(intent_id='I1', owner='ue1-video', priority=1, weight=3.0, ue_id='425',
                   requirement=Requirement(req_id='I1.r1', kpi='dlGoodputMbps', op='>=',
                                           value=0.8, unit='Mbps', scope='ue@425',
                                           bound=0.4, steps=2, relaxable=True)),
            Intent(intent_id='I4', owner='ue1-command', priority=1, weight=3.0, ue_id='425',
                   requirement=Requirement(req_id='I4.r1', kpi='deadlineSuccessRatio', op='>=',
                                           value=0.6, unit='ratio', scope='ue@425',
                                           bound=0.5, steps=1, relaxable=True,
                                           deadline_ms=200.0, deadline_bound=300.0,
                                           deadline_steps=1)),
        ]
        return Authorization.from_intents(intents)

    def answer(self, alternatives):
        auth = self.authorization_with_a_deadline()
        return {
            't0': {'targetId': 'T0',
                   'requirements': {r: auth.requirements[r].original for r in auth.req_ids}},
            'levels': {r: {'steps': auth.requirements[r].steps,
                           'bound': auth.requirements[r].limit} for r in auth.req_ids},
            'constraints': [], 'ranking': {}, 'alternatives': alternatives,
        }, auth

    def added(self, contract):
        ids = {row['targetId'] for row in
               dict(contract.provenance)['targetMembership']['modelAdditions']}
        return [t for t in contract.alternatives if t.target_id in ids]

    def test_an_unnamed_deadline_level_means_no_concession_on_it(self):
        document, auth = self.answer([{'levels': {'I1.r1': 1, 'I4.r1': 0}}])
        contract, _ = validate_target_contract(document, auth)
        carried = self.added(contract)[0]
        self.assertEqual({}, {k: v for k, v in dict(carried.deadline_levels).items() if v},
                         'an addition that did not ask to extend the window must not carry it')

    def test_naming_the_deadline_level_reaches_the_other_twin(self):
        document, auth = self.answer([{'levels': {'I1.r1': 1, 'I4.r1': 0},
                                       'deadlineLevels': {'I4.r1': 1}}])
        contract, _ = validate_target_contract(document, auth)
        self.assertEqual(1, dict(self.added(contract)[0].deadline_levels).get('I4.r1'))

    def test_the_pair_are_different_targets_and_leaving_the_window_alone_ranks_first(self):
        document, auth = self.answer([{'levels': {'I1.r1': 1, 'I4.r1': 0}},
                                      {'levels': {'I1.r1': 1, 'I4.r1': 0},
                                       'deadlineLevels': {'I4.r1': 1}}])
        contract, _ = validate_target_contract(document, auth)
        pair = self.added(contract)
        self.assertEqual(2, len(pair))
        self.assertEqual([0, 1], [dict(t.deadline_levels).get('I4.r1', 0) for t in pair])
        self.assertLess(preference_key(pair[0], auth), preference_key(pair[1], auth))

    def test_threshold_only_answers_never_extend_the_window(self):
        document, auth = self.answer([{'levels': {'I1.r1': 1, 'I4.r1': 0}},
                                      {'levels': {'I1.r1': 0, 'I4.r1': 1}}])
        contract, _ = validate_target_contract(document, auth)
        self.assertTrue(all(not any(dict(t.deadline_levels).values())
                            for t in self.added(contract)))


class TheModelsOwnOrderIsRecorded(unittest.TestCase):
    """``T`` is ranked by the owner's preference, so the answer's order is
    overwritten on the way in.  The additions' own sequence is recorded beside
    the contract, and never read back to rank anything."""

    def selection_of(self, contract):
        return dict(contract.provenance or {}).get('modelSelection')

    def test_the_answered_order_is_kept_while_T_stays_in_owner_order(self):
        # Dearer first, cheaper second: the reverse of the owner's ranking.
        document, auth = answer([{'levels': {'I1.r1': 2, 'I2.r1': 1, 'I3.r1': 2}},
                                 {'levels': {'I1.r1': 0, 'I2.r1': 0, 'I3.r1': 1}}])
        contract, _ = validate_target_contract(document, auth)
        keys = [preference_key(t, auth) for t in contract.alternatives]
        self.assertEqual(keys, sorted(keys))
        recorded = self.selection_of(contract)
        self.assertEqual([{'I1.r1': 2, 'I2.r1': 1, 'I3.r1': 2},
                          {'I1.r1': 0, 'I2.r1': 0, 'I3.r1': 1}],
                         [row['levels'] for row in recorded['order']])
        ids = [row['targetId'] for row in recorded['order']]
        self.assertGreater(ids[0], ids[1], 'named dearest first, so the ids descend')

    def test_mandatory_membership_is_never_attributed_to_the_model(self):
        document, auth = answer([{'levels': {'I1.r1': 1, 'I2.r1': 0, 'I3.r1': 0}}])
        contract, _ = validate_target_contract(document, auth)
        membership = dict(contract.provenance)['targetMembership']
        self.assertEqual(['T0'], [row['targetId'] for row in membership['mandatory']][:1])
        self.assertEqual(2, len(membership['mandatory']))
        self.assertEqual(1, len(membership['modelAdditions']))

    def test_the_deadline_axis_is_part_of_the_recorded_identity(self):
        pair = TheDeadlineAxisIsPartOfTheIdentity()
        document, auth = pair.answer([{'levels': {'I1.r1': 1, 'I4.r1': 0},
                                       'deadlineLevels': {'I4.r1': 1}},
                                      {'levels': {'I1.r1': 1, 'I4.r1': 0}}])
        contract, _ = validate_target_contract(document, auth)
        self.assertEqual([{'I4.r1': 1}, {'I4.r1': 0}],
                         [row['deadlineLevels'] for row in self.selection_of(contract)['order']])

    def test_it_survives_serialization(self):
        document, auth = answer([{'levels': {'I1.r1': 2, 'I2.r1': 1, 'I3.r1': 2}},
                                 {'levels': {'I1.r1': 0, 'I2.r1': 0, 'I3.r1': 1}}])
        contract, _ = validate_target_contract(document, auth)
        record = json.loads(json.dumps(contract.to_record()))
        provenance = record['provenance']
        self.assertEqual(2, len(provenance['modelSelection']['order']))
        self.assertIn('expressionError', provenance)
        self.assertIn('targetMembership', provenance)

    def test_an_explicitly_empty_selection_records_no_additions(self):
        document, auth = answer([])
        contract, _ = validate_target_contract(document, auth)
        self.assertEqual([], self.selection_of(contract)['order'])


class TheReasonAndEvidenceAreAdvisory(unittest.TestCase):
    """Recorded beside the order; they decide nothing."""

    CHOSEN = [{'levels': {'I1.r1': 2, 'I2.r1': 1, 'I3.r1': 2},
               'reason': 'the largest authorized concession short of the boundary',
               'evidenceRefs': ['E1']},
              {'levels': {'I1.r1': 0, 'I2.r1': 0, 'I3.r1': 1},
               'reason': 'ue3 is the incumbent and concedes cheapest',
               'evidenceRefs': ['E2', 'E3']}]

    def contract_for(self, alternatives):
        document, auth = answer(alternatives)
        return validate_target_contract(document, auth)[0]

    def test_they_reach_the_record_in_the_models_own_order(self):
        order = dict(self.contract_for(self.CHOSEN).provenance)['modelSelection']['order']
        self.assertEqual([c['reason'] for c in self.CHOSEN], [row['reason'] for row in order])
        self.assertEqual([['E1'], ['E2', 'E3']], [row['evidenceRefs'] for row in order])

    def test_an_absent_reason_refuses_nothing_and_is_never_invented(self):
        bare = [{'levels': dict(row['levels'])} for row in self.CHOSEN]
        order = dict(self.contract_for(bare).provenance)['modelSelection']['order']
        self.assertEqual([None, None], [row['reason'] for row in order])
        self.assertEqual([None, None], [row['evidenceRefs'] for row in order])

    def test_T_is_identical_with_and_without_the_advice(self):
        bare = [{'levels': dict(row['levels'])} for row in self.CHOSEN]
        self.assertEqual([t.requirements for t in self.contract_for(self.CHOSEN).targets],
                         [t.requirements for t in self.contract_for(bare).targets])


if __name__ == '__main__':
    unittest.main()


class ADeadlineAxisRowIsReadAsTheDeadlineHalf(TheDeadlineAxisIsPartOfTheIdentity):
    """2026-09-15 attempt 28: the Target model wrote the deadline half as its own
    ``I4.r1#deadline`` levels row -- this module's axis spelling -- and the refusal
    cost a repair call that ended the sitting on the formation deadline."""

    def test_the_axis_row_folds_into_its_requirement(self):
        document, auth = self.answer([{'levels': {'I1.r1': 1, 'I4.r1': 0}}])
        document['levels']['I4.r1#deadline'] = {'steps': 1, 'bound': 300.0}
        contract, _ = validate_target_contract(document, auth)
        self.assertTrue(contract.alternatives)

    def test_an_unknown_requirement_behind_the_suffix_is_still_refused(self):
        document, auth = self.answer([{'levels': {'I1.r1': 1, 'I4.r1': 0}}])
        document['levels']['I9.r1#deadline'] = {'steps': 1, 'bound': 300.0}
        with self.assertRaises(Exception) as caught:
            validate_target_contract(document, auth)
        self.assertIn('unknown requirement', str(caught.exception))

    def test_a_row_that_already_carries_its_deadline_half_keeps_the_refusal(self):
        document, auth = self.answer([{'levels': {'I1.r1': 1, 'I4.r1': 0}}])
        document['levels']['I4.r1'] = dict(document['levels']['I4.r1'], deadlineSteps=1, deadlineBound=300.0)
        document['levels']['I4.r1#deadline'] = {'steps': 1, 'bound': 250.0}
        with self.assertRaises(Exception) as caught:
            validate_target_contract(document, auth)
        self.assertIn('unknown requirement', str(caught.exception))
