"""`LiveTiming` 의 한도를 운영자가 실측에 맞춰 덮을 수 있어야 한다.

기본 `enforced_timeout_ms = 10_000` 은 **실측 확증 시간보다 짧았다.**  2026-09-23 실기:
조종 정책 하나를 혼자 쏘았을 때 KPM 이 목표 셀을 보기까지 6.0초, 프로듀서 readback 이
VERIFIED 가 되기까지 7.8초.  여유 2.2초인데 판은 9축을 한 트랜잭션으로 쓰므로 앞의 축들이
그 여유를 먹고, 조종 시행이 매번 `PARTIAL_APPLY 9/9 acknowledged (readback confirmed only
2)` 로 판정돼 롤백됐다 -- 그 롤백이 절반쯤 넘어간 UE 를 붕 띄웠다.
"""
import os
import unittest
from pathlib import Path
from unittest import mock

from tools.liveconsole.agent import LiveConsoleError, _live_timing_overrides
from assurance.live.pin_to_cell_driver import LiveTiming


class TestLiveTimingOverrides(unittest.TestCase):
    def test_no_environment_changes_nothing(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_live_timing_overrides(), {})
            self.assertEqual(LiveTiming(**_live_timing_overrides()).enforced_timeout_ms, 10_000)

    def test_the_action_deadline_can_be_raised(self):
        with mock.patch.dict(os.environ, {"AIC_ENFORCED_TIMEOUT_MS": "30000"}, clear=True):
            timing = LiveTiming(**_live_timing_overrides())
        self.assertEqual(timing.enforced_timeout_ms, 30_000)
        # 다른 값은 건드리지 않는다
        self.assertEqual(timing.freshness_bound_ms, LiveTiming().freshness_bound_ms)

    def test_an_empty_value_is_not_an_override(self):
        with mock.patch.dict(os.environ, {"AIC_ENFORCED_TIMEOUT_MS": "  "}, clear=True):
            self.assertEqual(_live_timing_overrides(), {})

    def test_a_non_number_is_refused_by_name_not_ignored(self):
        """조용히 무시하면 운영자는 자기가 준 값이 쓰인 줄 안다."""
        with mock.patch.dict(os.environ, {"AIC_ENFORCED_TIMEOUT_MS": "30s"}, clear=True):
            with self.assertRaises(LiveConsoleError) as caught:
                _live_timing_overrides()
        self.assertIn("AIC_ENFORCED_TIMEOUT_MS", str(caught.exception))

    def test_a_non_positive_value_is_refused(self):
        with mock.patch.dict(os.environ, {"AIC_ENFORCED_TIMEOUT_MS": "0"}, clear=True):
            with self.assertRaises(LiveConsoleError):
                _live_timing_overrides()

    def test_every_named_field_exists_on_LiveTiming(self):
        """오타 난 이름은 TypeError 로 판을 죽인다 -- 표와 dataclass 를 묶어 둔다."""
        from tools.liveconsole.agent import _LIVE_TIMING_ENV
        fields = set(LiveTiming.__dataclass_fields__)
        self.assertLessEqual(set(_LIVE_TIMING_ENV.values()), fields)

    def test_the_runner_sets_it_above_the_measured_corroboration(self):
        script = (Path(__file__).resolve().parents[1]
                  / "experiment_results/ota-20260911/ops/run_formal_v3.sh").read_text()
        self.assertIn("AIC_ENFORCED_TIMEOUT_MS", script)
        # 실측 7.8초보다 넉넉해야 한다
        import re
        found = re.search(r'AIC_ENFORCED_TIMEOUT_MS:-(\d+)', script)
        self.assertIsNotNone(found)
        self.assertGreaterEqual(int(found.group(1)), 15_000)


class TestTheHarmBoundCarriesTheOverride(unittest.TestCase):
    """정책의 `actionDeadlineMs` 는 **objective bundle 의 harm bound** 에서 온다.

    `LiveTiming` 이 아니다 -- `assurance/live/pin_to_cell_driver.live_contract_set` 은
    테스트 전용이고, 라이브 joint 경로는 `objectives/traffic_steering._bundle` 이 만든
    bound 를 쓴다.  거기에 10000 이 **리터럴로 박혀 있어서** 환경을 30초로 줘도 정책은
    10초 그대로였다 (2026-09-23, 판 184654 의 정책이 `"actionDeadlineMs": 10000`).
    """

    def test_the_default_is_the_declared_one(self):
        from assurance.contracts.actuation_request import (
            DEFAULT_ENFORCED_TIMEOUT_MS, enforced_timeout_ms_default)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(enforced_timeout_ms_default(), DEFAULT_ENFORCED_TIMEOUT_MS)

    def test_the_environment_raises_it(self):
        from assurance.contracts.actuation_request import enforced_timeout_ms_default
        with mock.patch.dict(os.environ, {"AIC_ENFORCED_TIMEOUT_MS": "30000"}, clear=True):
            self.assertEqual(enforced_timeout_ms_default(), 30_000)

    def test_a_bad_value_is_refused_not_silently_defaulted(self):
        from assurance.contracts.actuation_request import enforced_timeout_ms_default
        for bad in ("30s", "-1", "0"):
            with mock.patch.dict(os.environ, {"AIC_ENFORCED_TIMEOUT_MS": bad}, clear=True):
                with self.assertRaises(ValueError, msg=bad):
                    enforced_timeout_ms_default()

    def test_the_two_paths_share_one_environment_name(self):
        """한 판에서 두 경로가 다른 한도를 쓰면 무엇이 시간을 먹었는지 못 가린다."""
        from assurance.contracts.actuation_request import ENFORCED_TIMEOUT_ENV
        from tools.liveconsole.agent import _LIVE_TIMING_ENV
        self.assertIn(ENFORCED_TIMEOUT_ENV, _LIVE_TIMING_ENV)
        self.assertEqual(_LIVE_TIMING_ENV[ENFORCED_TIMEOUT_ENV], "enforced_timeout_ms")

    def test_no_harm_bound_anywhere_still_hardcodes_the_old_literal(self):
        """**형제 자리를 전부** 묶어야 한다.

        `_enforced_timeout_ms` 는 한 harm 계약의 bound 중 **가장 짧은 것**을 고른다.
        그래서 한 자리만 10초로 남아도 나머지를 30초로 올린 것이 전부 무효가 된다 --
        2026-09-23 에 traffic_steering 과 joint 만 고쳤더니 정책은 계속
        `actionDeadlineMs: 10000` 이었고, 범인은 `ue_level_target.py` 한 줄이었다.
        """
        import re
        root = Path(__file__).resolve().parents[1] / "assurance"
        offenders = []
        for path in root.rglob("*.py"):
            if "test" in str(path):
                continue
            text = path.read_text()
            for found in re.finditer(r"CertifiedHarmBound\(", text):
                segment = text[found.start():found.start() + 900]
                end = segment.find("\n    )")
                body = segment[:end if end > 0 else 900]
                if re.search(r"^\s*(10000|10_000),\s*$", body, re.M):
                    offenders.append(str(path.relative_to(root.parent)))
        self.assertEqual(offenders, [],
                         "harm bound 에 10초가 박혀 있으면 그것이 최솟값으로 이긴다")

    def test_the_shortest_bound_wins(self):
        """규칙 자체를 못 박는다 -- 이것이 위 검사가 필요한 이유다."""
        from assurance.contracts.actuation_request import _enforced_timeout_ms
        harm = type("H", (), {"contract_id": "harm/x", "bounds": (
            type("B", (), {"enforced_timeout_ms": 30_000})(),
            type("B", (), {"enforced_timeout_ms": 10_000})())})()
        self.assertEqual(_enforced_timeout_ms(harm), 10_000)


if __name__ == "__main__":
    unittest.main()
