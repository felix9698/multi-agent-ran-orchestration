"""Hardware-free calibration table and prompt provenance."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.campaign5.calibrate_agent_latency import calibrate, main, ROLES, role_prompts, percentile


class CalibrationTests(unittest.TestCase):
    @patch('tools.campaign5.calibrate_agent_latency.LLMBackendManager', side_effect=AssertionError('network discovery'))
    def test_mock_calibration_writes_all_roles_and_budget_signal(self, manager):
        with tempfile.TemporaryDirectory() as tmp:
            path = calibrate(['mock:agent'], ['100', '4000'], 2, tmp)
            self.assertEqual(path, Path(tmp) / 'agent-latency-calibration.json')
            rows = json.loads(path.read_text())['mock:agent']
            self.assertEqual(set(rows), set(ROLES))
            for role in ROLES:
                self.assertEqual(rows[role]['4000']['n'], 2)
                self.assertEqual(rows[role]['4000']['accepted'], 1)
                self.assertGreater(rows[role]['4000']['p50'], rows[role]['100']['p50'] + 40)

    def test_cli_accepts_reasoning_efforts(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(main(['--models', 'mock:agent', '--budgets', 'low,medium', '--repeat', '1', '--runs-root', tmp]), 0)
            self.assertIn('medium', json.loads((Path(tmp) / 'agent-latency-calibration.json').read_text())['mock:agent']['target'])

    def test_prompts_are_verbatim_and_selection_prompts_equal(self):
        text = Path('orc_task/SINGLE_CALL.md').read_text()
        prompts = role_prompts()
        self.assertEqual(prompts['trajectory'], prompts['monolith-select'])
        for prompt in prompts.values():
            self.assertIn(prompt, text)
            self.assertNotIn('##', prompt)

    def test_interpolated_percentile(self):
        self.assertEqual(percentile([10, 20, 30], .5), 20)
        self.assertEqual(percentile([10, 20, 30], .95), 29)
