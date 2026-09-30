"""거절이 관측됐으면 그 말을 그대로 실어야 한다.

2026-09-23: `_refused_without_writing` 이 bool 이라 producer 의
`AIC_E2_NOT_READY / ADMISSION` 을 버렸고, 판은 이유 칸이 빈
`PARTIAL_APPLY 9/9 acknowledged (the readback confirmed none)` 로만 죽었다.
그 빈칸 때문에 추적이 `actionDeadlineMs` 라는 엉뚱한 층으로 갔다.
"""
import unittest
from unittest.mock import Mock

from assurance.gateway.r1_adapter import R1Adapter


def _adapter(status):
    a = object.__new__(R1Adapter)
    a._port = Mock(get_policy_status=Mock(return_value=status))
    return a


class TestRefusalIsQuoted(unittest.TestCase):
    def test_a_refusal_without_a_write_names_stage_and_code(self):
        a = _adapter({"aicStatus": {"error": {
            "writeMayHaveOccurred": False,
            "stage": "ADMISSION",
            "code": "AIC_E2_NOT_READY",
        }}})
        self.assertEqual(a._refused_without_writing("p"), "ADMISSION: AIC_E2_NOT_READY")

    def test_a_write_that_may_have_occurred_is_not_a_refusal(self):
        for flag in (True, None, "false"):
            a = _adapter({"aicStatus": {"error": {"writeMayHaveOccurred": flag,
                                                  "code": "X"}}})
            self.assertIsNone(a._refused_without_writing("p"), flag)

    def test_an_unreadable_status_proves_nothing(self):
        a = object.__new__(R1Adapter)
        a._port = Mock(get_policy_status=Mock(side_effect=RuntimeError("no")))
        self.assertIsNone(a._refused_without_writing("p"))

    def test_a_status_without_an_error_is_not_a_refusal(self):
        self.assertIsNone(_adapter({"aicStatus": {}})._refused_without_writing("p"))
        self.assertIsNone(_adapter({})._refused_without_writing("p"))

    def test_missing_stage_or_code_still_returns_a_sentence(self):
        a = _adapter({"aicStatus": {"error": {"writeMayHaveOccurred": False}}})
        self.assertEqual(a._refused_without_writing("p"), "?: ?")


if __name__ == "__main__":
    unittest.main()
