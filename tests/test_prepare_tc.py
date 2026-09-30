"""Re-keying a banked T and C onto another attempt's UE identities.

Hermetic: no radio, no model, no runner. The board records are the shapes the
episode already writes, trimmed to what the substitution has to survive.
"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ENTRY = (Path(__file__).resolve().parents[1]
         / 'experiment_results/ota-20260911/prepare_tc.py')


def load():
    spec = importlib.util.spec_from_file_location('_prepare_tc_under_test', ENTRY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EPISODE = {
    'episodeId': 'case/liveconsole-agent:20260911T142823Z',
    'ueHosts': {'424': 'ue3', '425': 'ue1', '426': 'ue2'},
    'timing': {'prepMs': 121239.0},
    'T': {'schemaVersion': 'agent-target-contract/1.1.0',
          't0': {'targetId': 'T0', 'requirements': {'I1.r1': 0.8}},
          'authorization': {'I1.r1': {'scope': 'ue@425', 'original': 0.8}},
          'alternatives': [{'targetId': 'T1', 'requirements': {'I1.r1': 0.6}}]},
    'C': {'schemaVersion': 'agent-control-candidates/1.1.0',
          'candidates': [{'controlId': 'C0',
                          'configuration': {'servingCell@424': '12345678',
                                            'servingCell@425': '87654321',
                                            'dlPrbCap@426': '0'}}],
          'actionSpace': {'servingCell@424': ['12345678', '87654321']}},
}


class Rekeying(unittest.TestCase):
    def setUp(self):
        self.module = load()

    def test_every_occurrence_of_an_id_moves_together(self):
        out = self.module.rekey(EPISODE['C'], {'424': '457', '425': '459', '426': '458'})
        self.assertEqual({'servingCell@457': '12345678', 'servingCell@459': '87654321',
                          'dlPrbCap@458': '0'}, out['candidates'][0]['configuration'])
        self.assertIn('servingCell@457', out['actionSpace'])

    def test_a_swap_is_not_carried_into_the_next_one(self):
        # 424->425 and 425->426 applied in sequence would turn the first into 426.
        out = self.module.rekey({'a': 'x@424', 'b': 'x@425'}, {'424': '425', '425': '426'})
        self.assertEqual({'a': 'x@425', 'b': 'x@426'}, out)

    def test_an_id_inside_a_longer_number_is_left_alone(self):
        out = self.module.rekey({'a': '1424', 'b': '4240', 'c': 424, 'd': '424'},
                                {'424': '457'})
        self.assertEqual({'a': '1424', 'b': '4240', 'c': 424, 'd': '457'}, out)

    def test_an_empty_mapping_returns_the_board_unchanged(self):
        board = EPISODE['T']
        self.assertEqual(board, self.module.rekey(board, {}))

    def test_scopes_inside_the_authorization_are_rekeyed_too(self):
        out = self.module.rekey(EPISODE['T'], {'425': '459'})
        self.assertEqual('ue@459', out['authorization']['I1.r1']['scope'])
        self.assertEqual(0.8, out['t0']['requirements']['I1.r1'])


class TheCommandLineForm(unittest.TestCase):
    def setUp(self):
        self.module = load()
        self.directory = Path(tempfile.mkdtemp())
        self.episode = self.directory / 'episode.json'
        self.episode.write_text(json.dumps(EPISODE), encoding='utf-8')

    def test_it_writes_a_board_main_py_can_load(self):
        out = self.directory / 'prepared.json'
        self.module.main([str(self.episode), 'ue1=459,ue2=458,ue3=457', str(out)])
        document = json.loads(out.read_text(encoding='utf-8'))
        self.assertEqual({'T', 'C', 'preparedFrom'}, set(document))
        self.assertEqual({'424': '457', '425': '459', '426': '458'},
                         document['preparedFrom']['rekeyed'])
        self.assertEqual(121239.0, document['preparedFrom']['prepMs'])
        self.assertEqual('servingCell@459',
                         [k for k in document['C']['candidates'][0]['configuration']
                          if k.endswith('459')][0])

    def test_a_host_the_episode_never_held_is_refused_by_name(self):
        with self.assertRaisesRegex(SystemExit, 'ue4'):
            self.module.main([str(self.episode), 'ue4=1', str(self.directory / 'x.json')])

    def test_a_host_whose_id_is_unchanged_produces_no_swap(self):
        out = self.directory / 'same.json'
        self.module.main([str(self.episode), 'ue1=425', str(out)])
        self.assertEqual({}, json.loads(out.read_text())['preparedFrom']['rekeyed'])



class TheRunnerSeam(unittest.TestCase):
    """``prepared_board`` is what puts ``--prepared-tc`` on the CLI, or nothing."""

    def setUp(self):
        import importlib.util
        import sys
        directory = (Path(__file__).resolve().parents[1]
                     / 'experiment_results/ota-20260911')
        if str(directory) not in sys.path:
            sys.path.insert(0, str(directory))
        spec = importlib.util.spec_from_file_location(
            '_runner_under_test', directory / 'atomic_formal_run_guarded.py')
        self.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runner)
        self.root = Path(tempfile.mkdtemp())
        self.ids = {'ue1': '459', 'ue2': '458', 'ue3': '457'}
        self.banked = self.root / 'episode.json'
        self.banked.write_text(json.dumps(EPISODE), encoding='utf-8')

    def _with(self, value):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {'AIC_PREPARED_BOARD': value}):
            return self.runner.prepared_board(self.root, self.ids)

    def test_no_board_named_adds_no_flag(self):
        self.assertEqual((), self._with(''))

    def test_a_named_board_that_is_absent_adds_no_flag(self):
        self.assertEqual((), self._with(str(self.root / 'nope.json')))

    def test_a_banked_board_becomes_the_flag_and_a_rekeyed_file(self):
        argv = self._with(str(self.banked))
        self.assertEqual('--prepared-tc', argv[0])
        document = json.loads(Path(argv[1]).read_text(encoding='utf-8'))
        self.assertEqual({'424': '457', '425': '459', '426': '458'},
                         document['preparedFrom']['rekeyed'])
        self.assertIn('servingCell@459', document['C']['candidates'][0]['configuration'])
        self.assertEqual('ue@459', document['T']['authorization']['I1.r1']['scope'])

if __name__ == '__main__':
    unittest.main()
