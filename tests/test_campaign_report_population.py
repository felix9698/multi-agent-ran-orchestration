"""v4.7 Codex #16/#18: the report's population is the start ledger, and the paper metric is
read from paperEvaluation.best -- a started board without a record is still counted."""
import importlib.util
import json
import os
import tempfile
import unittest

_PATH = os.path.join(os.path.dirname(__file__), '..', 'experiment_results', 'ota-20260911',
                     'ops', 'campaign_report.py')
_spec = importlib.util.spec_from_file_location('campaign_report', _PATH)
report_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report_mod)


class PopulationFromLedger(unittest.TestCase):
    def test_missing_record_stays_in_population(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, 'ledger.jsonl')
            with open(ledger, 'w') as fh:
                fh.write(json.dumps({'campaign': 'c', 'episodeStarted': True, 'method': 'basic-monolith',
                                     'attempt': 1, 'dir': 'no-such-dir'}) + '\n')
                fh.write(json.dumps({'campaign': 'c', 'episodeStarted': False, 'attempt': 2}) + '\n')
                fh.write(json.dumps({'campaign': 'other', 'episodeStarted': True, 'attempt': 3}) + '\n')
            started, rows, missing = report_mod.load('c', ledger)
        self.assertEqual((len(started), len(rows), len(missing)), (1, 0, 1))

    def test_paper_metric_reads_best(self):
        rows = [{'paperEvaluation': {'ownerPriority': ['a', 'b'],
                                     'best': {'t0': True, 'ownerBins': [0, 0]}}},
                {'paperEvaluation': {'ownerPriority': ['a', 'b'],
                                     'best': {'t0': False, 'ownerBins': [2, 4]}}},
                {'paperEvaluation': {'ownerPriority': ['a', 'b'], 'best': None}},
                {}]
        pm = report_mod.paper_metric(rows)
        self.assertEqual((pm['attained'], pm['t0'], pm['noEvaluation']), (2, 1, 1))
        self.assertEqual(pm['meanBins'], [1.0, 2.0])


if __name__ == '__main__':
    unittest.main()
