"""블록 캠페인 계획 — 순열이 고르게 들어가고 시드가 재현되는가.

2026-09-23 결정 §5.2: 한 블록 = 연속 세 판(방식마다 하나), 6가지 순서 순열을 각 3회,
18블록 54판, 기록된 시드로 섞는다.  **왜**: 이 베드의 셀 총량은 몇 시간 주기로 약
5 Mbps 진동하므로(v4.4 전수 이동 중앙값 11.0~15.9) 방식마다 판이 도는 시각이 다르면
그 변동이 방식 차이로 둔갑한다.
"""
import itertools
import json
import random
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPT = (Path(__file__).resolve().parents[1]
           / 'experiment_results/ota-20260911/ops/run_blocks_campaign.sh')
METHODS = ("three-agent", "internal-monolith", "basic-monolith")


def _plan(blocks: int, seed: int):
    """계획 생성기를 스크립트에서 그대로 떼어 돌린다."""
    perms = list(itertools.permutations(METHODS))
    if blocks % len(perms):
        raise ValueError('not a multiple')
    plan = [list(p) for p in perms for _ in range(blocks // len(perms))]
    random.Random(seed).shuffle(plan)
    return plan


class TestPlan(unittest.TestCase):
    def test_eighteen_blocks_give_each_permutation_three_times(self):
        import collections
        counts = collections.Counter(tuple(b) for b in _plan(18, 20260923))
        self.assertEqual(sorted(counts.values()), [3] * 6)

    def test_every_block_holds_each_method_once(self):
        for block in _plan(18, 20260923):
            self.assertEqual(sorted(block), sorted(METHODS))

    def test_the_seed_reproduces_the_order(self):
        self.assertEqual(_plan(18, 20260923), _plan(18, 20260923))

    def test_a_different_seed_gives_a_different_order(self):
        self.assertNotEqual(_plan(18, 20260923), _plan(18, 1))

    def test_a_block_count_that_is_not_a_multiple_of_six_is_refused(self):
        with self.assertRaises(ValueError):
            _plan(17, 20260923)

    def test_fifty_four_episodes(self):
        self.assertEqual(sum(len(b) for b in _plan(18, 20260923)), 54)


class TestScript(unittest.TestCase):
    def test_the_script_parses(self):
        result = subprocess.run(['bash', '-n', str(_SCRIPT)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode()[:300])

    def test_a_slot_advances_only_on_a_started_episode(self):
        """preflight 거절이 그 자리를 소모하면 거절이 잦은 시각의 방식만 표본을 잃는다."""
        source = _SCRIPT.read_text()
        self.assertIn("if started:", source)
        self.assertIn("state['slot'] = slot + 1", source)

    def test_the_plan_is_written_once_and_then_read(self):
        source = _SCRIPT.read_text()
        self.assertIn('[ -f "$PLAN" ] ||', source,
                      '재기동마다 새로 섞으면 기록된 시드가 뜻을 잃는다')

    def test_the_pause_switch_is_honoured(self):
        self.assertIn('overnight/PAUSE', _SCRIPT.read_text())


if __name__ == '__main__':
    unittest.main()


class TheRunnerRecordsItsInterruptions(unittest.TestCase):
    """결정 §5.2 "Report any infrastructure interruption and incomplete block."

    `incompleteBlocks` 는 만들어만 두고 한 번도 쓰이지 않았다 (2026-09-23 조사).
    """

    @staticmethod
    def _snippet() -> str:
        text = _SCRIPT.read_text(encoding='utf-8')
        match = re.search(r"<<'STATE_PY'\n(.*?)\nSTATE_PY\n", text, re.S)
        assert match, 'the start-recording block is missing from the runner'
        return match.group(1)

    def _start(self, state_path: str) -> dict:
        subprocess.run([sys.executable, '-', state_path], input=self._snippet(),
                       text=True, check=True)
        return json.loads(Path(state_path).read_text())

    def test_first_start_then_a_mid_block_resume_is_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'progress.json')
            Path(path).write_text(json.dumps(
                {"block": 0, "slot": 0, "completedBlocks": 0, "incompleteBlocks": []}))
            state = self._start(path)
            self.assertEqual(['runner-start'], [e['kind'] for e in state['runnerStarts']])
            self.assertEqual([], state['incompleteBlocks'])
            state.update(block=4, slot=1)
            Path(path).write_text(json.dumps(state))
            state = self._start(path)
            self.assertEqual('resumed-mid-block', state['runnerStarts'][-1]['kind'])
            self.assertEqual([4], state['incompleteBlocks'])

    def test_a_resume_at_a_block_boundary_is_an_interruption_but_not_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'progress.json')
            Path(path).write_text(json.dumps(
                {"block": 2, "slot": 0, "completedBlocks": 2, "incompleteBlocks": [],
                 "runnerStarts": [{"kind": "runner-start"}]}))
            state = self._start(path)
            self.assertEqual('resumed-at-block-boundary', state['runnerStarts'][-1]['kind'])
            self.assertEqual([], state['incompleteBlocks'])
