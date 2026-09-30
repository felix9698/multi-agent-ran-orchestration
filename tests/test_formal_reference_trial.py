"""기준 관측은 공짜가 아니다.

2026-09-23 결정 §5.1: 그 관측도 준비 뒤에 일어나고 관측 구간을 쓴다.  이전 판들은
`counted: false` · `elapsed 0` 으로 적어서, T0_MET 8판의 달성 시각이 0 인데 실제 창
종료는 `timing.t0` 에서 **23~97초**(중앙 76.9초) 뒤였다.  켜면 `N_max` 의 한 칸을 쓰고
실제 경과를 적는다.  그리고 그 결말은 **제어가 만든 해결이 아니다**.
"""
import inspect
import re
import unittest
from pathlib import Path

from tools.liveconsole.agent import AgentRequest, AgentSitting

_RUNNER = (Path(__file__).resolve().parents[1]
           / 'experiment_results/ota-20260911/atomic_formal_run_guarded.py')


class TestFormalReferenceTrial(unittest.TestCase):
    def test_the_flag_defaults_off_so_old_counting_is_unchanged(self):
        import dataclasses
        field = next(f for f in dataclasses.fields(AgentRequest)
                     if f.name == 'formal_reference_trial')
        self.assertIs(field.default, False,
                      '기본이 켜져 있으면 그 전 판과 셈이 조용히 달라진다')

    def test_the_initial_measurement_counts_only_when_the_flag_is_on(self):
        source = inspect.getsource(AgentSitting._initial_measurement)
        self.assertIn('["counted"] = bool(', source)
        self.assertIn('self.request.formal_reference_trial', source)

    def test_its_elapsed_is_real_when_the_flag_is_on(self):
        source = inspect.getsource(AgentSitting._initial_measurement)
        self.assertIn('self._elapsed_ms() if self.request.formal_reference_trial else 0.0',
                      source, 'elapsed 0 이 고정이면 시간축 그림을 그릴 수 없다')

    def test_the_outcome_is_not_called_a_control_induced_resolution(self):
        source = inspect.getsource(AgentSitting._initial_measurement)
        self.assertIn('initially_T0_satisfied', source)
        self.assertIn('before any control was applied', source)

    def test_the_runner_turns_it_on(self):
        source = _RUNNER.read_text()
        self.assertIn("'--formal-reference-trial',", source)

    def test_main_exposes_the_flag_and_wires_it(self):
        source = (Path(__file__).resolve().parents[1] / 'main.py').read_text()
        self.assertIn('--formal-reference-trial', source)
        self.assertIn('formal_reference_trial=bool(', source)


if __name__ == '__main__':
    unittest.main()
