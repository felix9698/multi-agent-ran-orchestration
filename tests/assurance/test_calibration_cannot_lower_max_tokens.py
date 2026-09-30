"""보정은 생각 예산을 깎는 장치이지 출력 상한을 깎는 장치가 아니다 (2026-09-21).

`maxTokens` 는 지연 노브가 아니라 **절단선**이다.  보정의 일은 관측이 상하기 전에 답이
오게 하는 것이고, 그 목적에 출력 상한을 깎을 이유는 없다.

**이 시험이 지키는 것은 잠복 가드다.**  처음에는 "최근 40판의 `maxTokens: 3000` 이
보정표에서 나왔다" 고 적었는데 틀렸다 -- 운영자가 `--generation` 으로 값을 주면
`_SittingAgents.options_for()` 가 보정 경로에 들어가기 전에 반환한다.  그 3000 은 러너의
지시였다.  근거를 정정하되 가드는 남긴다: 운영자 지시가 없는 판에서는 보정표가 유일한
결정자가 되고, 그때 상한이 깎이면 아무도 눈치채지 못한다.
"""

from __future__ import annotations

import unittest

from assurance.coordination.intake import (
    GENERATION_DEFAULTS,
    GenerationOptions,
    choose_from_calibration,
)


def table(role: str, max_tokens: int, thinking: int, p95_ms: float):
    # 보정 문서의 실제 모양: 항목마다 옵션이 평평하게 놓인다 (`_calibration_rows`).
    return {role: [{"p95": p95_ms, "maxTokens": max_tokens,
                    "thinkingBudgetTokens": thinking,
                    "reasoningEffort": "medium", "jsonMode": True}]}


class CalibrationMayShrinkThinkingNotOutput(unittest.TestCase):
    def test_a_lower_calibrated_cap_never_lowers_the_role_default(self):
        default = GenerationOptions.for_role("target")
        self.assertEqual(4000, default.max_tokens)      # 표가 바뀌면 이 시험도 다시 보라
        chosen = choose_from_calibration(
            table("target", max_tokens=3000, thinking=0, p95_ms=1000.0),
            "target", min_validity_ms=10000)
        self.assertEqual(4000, chosen.max_tokens, "보정이 절단선을 되돌렸다")
        self.assertEqual(0, chosen.thinking_budget_tokens, "생각 예산은 깎을 수 있어야 한다")
        self.assertEqual("calibration", chosen.source)

    def test_a_higher_calibrated_cap_is_still_honoured(self):
        # 2026-09-23: 선택 호출의 기본이 1000 -> 2000 으로 올라(짝지은 선택기 예산을
        # 맞추기 위해) 시험값 2000 이 기본과 같아졌다.  이 시험의 뜻은 "보정이 더 높게
        # 부르면 그대로 존중한다" 이므로 시험값을 기본 위로 올려 그 뜻을 지킨다.
        higher = GENERATION_DEFAULTS["trajectory"]["maxTokens"] + 1000
        chosen = choose_from_calibration(
            table("trajectory", max_tokens=higher, thinking=0, p95_ms=1000.0),
            "trajectory", min_validity_ms=10000)
        self.assertEqual(higher, chosen.max_tokens)
        self.assertGreater(higher, GENERATION_DEFAULTS["trajectory"]["maxTokens"])

    def test_no_fitting_row_leaves_the_default_alone(self):
        chosen = choose_from_calibration(
            table("control", max_tokens=3000, thinking=8000, p95_ms=99999.0),
            "control", min_validity_ms=10000)
        self.assertEqual(GenerationOptions.for_role("control"), chosen)


if __name__ == "__main__":
    unittest.main()
