"""짝지은 선택기는 같은 출력 예산을 받아야 한다.

2026-09-23 (시나리오 검토 §2, 실측 확인): 3A 의 `trajectory` 와 IM 의
`monolith-select` 는 **같은 시스템 프롬프트**(sha256 753884081ea7)를 쓰면서 러너가
maxTokens 2000 대 1000 을 실어 보냈고 BM 은 1500 이었다.  같은 프롬프트에 2배 예산이면
"짝지은 선택기" 비교가 아니다.  결정: 선택 호출 전부 2000, 형성은 4000 유지.

**두 자리를 함께 봐야 한다** -- 기본 표(`GENERATION_DEFAULTS`)와 러너의 `--generation`.
러너가 표를 덮으므로 표만 고치면 판에는 반영되지 않는다.
"""
import re
import unittest
from pathlib import Path

from assurance.coordination.intake import GENERATION_DEFAULTS

SELECTION = ("trajectory", "monolith-select", "basic-monolith")
FORMATION = ("target", "control", "monolith-form")
_RUNNER = (Path(__file__).resolve().parents[1]
           / 'experiment_results/ota-20260911/atomic_formal_run_guarded.py')


class TestSelectionBudgetIsMatched(unittest.TestCase):
    def test_every_selection_role_gets_the_same_budget(self):
        budgets = {role: GENERATION_DEFAULTS[role]["maxTokens"] for role in SELECTION}
        self.assertEqual(set(budgets.values()), {2000}, budgets)

    def test_formation_keeps_its_larger_budget(self):
        for role in FORMATION:
            self.assertEqual(GENERATION_DEFAULTS[role]["maxTokens"], 4000, role)

    def test_the_runner_sends_the_same_budget_it_does_not_override_it_apart(self):
        """러너가 표를 덮으므로 여기도 봐야 한다."""
        source = _RUNNER.read_text()
        sent = dict(re.findall(r"'--generation', '([a-z-]+)=(\d+):", source))
        for role in SELECTION:
            self.assertEqual(sent.get(role), '2000',
                             f'{role} 이 러너에서 {sent.get(role)} 로 나간다')
        for role in FORMATION:
            self.assertEqual(sent.get(role), '4000', role)

    def test_the_table_and_the_runner_agree(self):
        """둘이 어긋나면 어느 값이 판에 실렸는지 기록으로만 알 수 있다."""
        source = _RUNNER.read_text()
        sent = dict(re.findall(r"'--generation', '([a-z-]+)=(\d+):", source))
        for role in SELECTION + FORMATION:
            self.assertEqual(int(sent[role]), GENERATION_DEFAULTS[role]["maxTokens"], role)


if __name__ == '__main__':
    unittest.main()
