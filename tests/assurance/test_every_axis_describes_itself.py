"""모델에 노출되는 축은 전부 스스로를 설명해야 한다 (2026-09-21).

여섯 축 중 `dlMcsBounds` 와 `slicePrbQuota` 만 설명 없이 프롬프트에 갔다.  그러면
모델이 그 둘을 덜 고르는 것이 **방식의 성질이 아니라 내 입력 결손**이 되고, 축 선택
분포로 무엇도 주장할 수 없게 된다.  전달 경로(`FunctionSpec.to_record`)는 원래
연결돼 있었으므로 빠진 것은 문장뿐이었다.
"""

from __future__ import annotations

import unittest

from assurance.coordination.tc import FUNCTION_NAMING


class EveryExposedAxisCarriesProse(unittest.TestCase):
    def test_no_axis_reaches_the_model_unexplained(self):
        missing = sorted(name for name, entry in FUNCTION_NAMING.items()
                         if not str(entry.get("description") or "").strip())
        self.assertEqual([], missing, "설명 없이 모델에 가는 축")

    def test_each_description_says_what_the_value_does(self):
        # 단위 이름만 되풀이하는 문장은 설명이 아니다 -- 길이로 싸게 거른다.
        for name, entry in FUNCTION_NAMING.items():
            with self.subTest(axis=name):
                self.assertGreater(len(entry["description"]), 40, name)
                self.assertTrue(entry.get("unit"), name)
                self.assertTrue(entry.get("prerequisites"), name)


if __name__ == "__main__":
    unittest.main()
