"""Forming T and C off the radio: the parts that do not need a model.

The script's whole point is to call Target and Control for real, so the calls
themselves are not covered here. What is covered is everything around them that
silently produced a useless board the first two times: the host mapping the
re-keying depends on, and the check that no candidate names an axis value the
live catalog will refuse.
"""
import importlib.util
import json
from pathlib import Path
import sys
import unittest

ENTRY = (Path(__file__).resolve().parents[1]
         / 'experiment_results/ota-20260911/form_board_offline.py')


def load():
    directory = str(ENTRY.parent)
    if directory not in sys.path:
        sys.path.insert(0, directory)
    spec = importlib.util.spec_from_file_location('_form_board_under_test', ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INTENTS = [
    {'intentId': 'I1', 'owner': 'ue1-video', 'ueId': '425'},
    {'intentId': 'I2', 'owner': 'ue2-map', 'ueId': '426'},
    {'intentId': 'I3', 'owner': 'ue3-incumbent', 'ueId': '424'},
    {'intentId': 'I4', 'owner': 'ue1-command', 'ueId': '425'},
]


def host_map(intents):
    """The mapping the script derives, reproduced from its own table."""
    owner_host = {'ue1-video': 'ue1', 'ue1-command': 'ue1',
                  'ue2-map': 'ue2', 'ue3-incumbent': 'ue3'}
    mapping = {}
    for intent in intents:
        host = owner_host.get(intent.get('owner'))
        if host:
            mapping[str(intent['ueId'])] = host
    return mapping


class TheHostMappingTheRekeyingNeeds(unittest.TestCase):
    def test_every_ue_id_is_named_with_its_host(self):
        self.assertEqual({'425': 'ue1', '426': 'ue2', '424': 'ue3'}, host_map(INTENTS))

    def test_two_intents_on_one_host_collapse_to_one_entry(self):
        # I1 and I4 are both ue1's, and they must not disagree.
        mapping = host_map(INTENTS)
        self.assertEqual('ue1', mapping['425'])
        self.assertEqual(3, len(mapping))

    def test_an_unknown_owner_leaves_a_host_missing(self):
        # This is what the script refuses on: without all three, prepare_tc has
        # nothing to swap and the board keeps the ids it was formed with.
        broken = [dict(i) for i in INTENTS]
        broken[2]['owner'] = 'someone-else'
        self.assertNotEqual(['ue1', 'ue2', 'ue3'],
                            sorted(set(host_map(broken).values())))

    def test_the_script_refuses_a_board_it_cannot_key(self):
        module = load()
        source = ENTRY.read_text(encoding='utf-8')
        self.assertIn('could not tell which host each ue id belongs to', source)
        self.assertTrue(hasattr(module, 'route_to_the_proxy'))


class TheAdmissibilityCheck(unittest.TestCase):
    """A candidate naming a value the live catalog does not admit must be seen here,
    not at injection time when the whole attempt is already running."""

    SPACE = {'servingCell@424': ('12345678', '87654321'),
             'dlPrbCap@424': ('0', '6')}

    def inadmissible(self, configuration):
        found = []
        for axis, value in configuration.items():
            if axis not in self.SPACE:
                found.append(f'no axis {axis}')
            elif str(value) not in self.SPACE[axis]:
                found.append(f'{axis}={value} not admitted')
        return found

    def test_an_admitted_configuration_is_clean(self):
        self.assertEqual([], self.inadmissible(
            {'servingCell@424': '12345678', 'dlPrbCap@424': '6'}))

    def test_a_cap_the_space_does_not_offer_is_caught(self):
        self.assertEqual(['dlPrbCap@424=12 not admitted'], self.inadmissible(
            {'servingCell@424': '12345678', 'dlPrbCap@424': '12'}))

    def test_an_axis_this_sitting_never_exposes_is_caught(self):
        self.assertEqual(['no axis pfWeight@424'], self.inadmissible(
            {'pfWeight@424': '0.5'}))

    def test_an_unkeyed_axis_from_another_cohort_is_caught(self):
        # The failure the first board actually had: ids never swapped, so every
        # axis key belonged to a cohort this sitting does not address.
        self.assertEqual(['no axis servingCell@425'],
                         self.inadmissible({'servingCell@425': '12345678'}))


class WhatTheBoardMustCarry(unittest.TestCase):
    def test_a_board_without_ue_hosts_cannot_be_rekeyed(self):
        spec = importlib.util.spec_from_file_location(
            '_prepare_tc_for_board', ENTRY.parent / 'prepare_tc.py')
        prepare = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(prepare)
        board = {'T': {'a': 'ue@425'}, 'C': {}, 'ueHosts': None}
        was = {host: ue for ue, host in (board.get('ueHosts') or {}).items()}
        swap = {was[h]: n for h, n in {'ue1': '901'}.items() if was.get(h)}
        self.assertEqual({}, swap)
        self.assertEqual({'a': 'ue@425'}, prepare.rekey(board['T'], swap))

    def test_a_board_with_ue_hosts_is_rekeyed_throughout(self):
        spec = importlib.util.spec_from_file_location(
            '_prepare_tc_for_board2', ENTRY.parent / 'prepare_tc.py')
        prepare = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(prepare)
        board = {'T': {'scope': 'ue@425'},
                 'C': {'configuration': {'dlPrbCap@425': '0'}},
                 'ueHosts': {'425': 'ue1'}}
        was = {host: ue for ue, host in board['ueHosts'].items()}
        swap = {was['ue1']: '901'}
        self.assertEqual({'scope': 'ue@901'}, prepare.rekey(board['T'], swap))
        self.assertEqual({'configuration': {'dlPrbCap@901': '0'}},
                         prepare.rekey(board['C'], swap))


if __name__ == '__main__':
    unittest.main()
