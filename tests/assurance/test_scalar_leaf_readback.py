"""The scalar readback spells a value the way the frozen axis does."""
import unittest

from assurance.gateway.live import scalar_leaf_readback
from assurance.gateway.plan import config_hash


def _readback(value):
    return lambda **_: {"pfWeight": {"pfWeight": value}}


class TheScalarReadbackSpellsTheFrozenAxis(unittest.TestCase):
    def read(self, value, **kwargs):
        project = scalar_leaf_readback(_readback(value), axis="pfWeight", leaf="pfWeight", **kwargs)
        return project(scope={}, transaction_id="tx", policy_id="p")

    def test_an_integral_producer_weight_reads_back_as_the_frozen_real(self):
        # 2026-09-14 OTA: the producer stored 4, the candidate froze "4.0".
        observed = self.read(4, render=lambda value: str(float(value)))
        self.assertEqual(observed, {"pfWeight": "4.0"})
        self.assertEqual(config_hash(observed), config_hash({"pfWeight": "4.0"}))

    def test_the_default_keeps_the_integer_axes_unchanged(self):
        self.assertEqual(self.read(12), {"pfWeight": "12"})

    def test_no_observation_stays_unobserved(self):
        self.assertIsNone(self.read(None, render=lambda value: str(float(value))))


class EveryRealValuedAxisKindSpellsItsDecimal(unittest.TestCase):
    """2026-09-15 의 수정이 형제 축에 가지 않아 2026-09-18 에 그대로 재발했다.

    그때 고친 조건은 ``declared.value_kind == "number"`` 라는 **정확 비교**였고,
    뒤에 들어온 감쇠 축의 종류는 ``"attenuation-db"`` 라 그 수정을 못 받았다.
    결과: 라디오는 3.0 dB 로 실제로 움직였고(KPM 3회 확인) 어댑터도 OBSERVED 로
    기록했는데, 게이트웨이의 관측 설정은 ``"3"`` 을 실어 계획의 ``"3.0"`` 과
    어느 prefix 에도 안 맞았고 `the readback confirmed none` 이 되었다.
    """

    def render_for(self, kind):
        from tools.liveconsole.build import REAL_VALUED_AXIS_KINDS
        return ((lambda value: str(float(value)))
                if kind in REAL_VALUED_AXIS_KINDS else str)

    def test_the_attenuation_axis_spells_an_integral_value_with_its_decimal(self):
        self.assertEqual(self.render_for("attenuation-db")(3), "3.0")

    def test_the_weight_axis_still_does_too(self):
        self.assertEqual(self.render_for("number")(4), "4.0")

    def test_an_integer_axis_is_left_alone(self):
        self.assertEqual(self.render_for("integer")(12), "12")

    def test_the_declared_attenuation_kind_is_the_one_in_the_set(self):
        """집합이 실제 선언과 어긋나면 이 검사가 먼저 깨진다."""
        from assurance.objectives.action102_support import (
            ATTENUATION_ACTION_ID, SUPPLEMENTARY_ACTIONS)
        from tools.liveconsole.build import REAL_VALUED_AXIS_KINDS
        declared = SUPPLEMENTARY_ACTIONS[ATTENUATION_ACTION_ID]
        self.assertIn(declared.value_kind, REAL_VALUED_AXIS_KINDS)


if __name__ == "__main__":
    unittest.main()
