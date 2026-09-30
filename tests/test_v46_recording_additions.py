"""결정 §4·§5.1 의 기록 항목 (2026-09-23).

§4 "Record truncation" -- 공급자 stop_reason 은 백엔드마다 달라, max_tokens 절단의
사실을 두 수로 적는다: 보고된 출력 토큰이 **전송된** 한도에 닿았는가.  모르면 None.

§5.1 "Record distinct timestamps for observation-window end, validity/evaluation
completion, retention confirmation, recovery completion, and search termination."
"""
import unittest


class TruncationIsRecordedFromTheTwoNumbers(unittest.TestCase):
    def _flag(self, out, limit):
        # 기록 규칙 자체를 시험한다 -- agents.py 의 같은 식.
        sent = None if limit is None else {"maxTokens": limit}
        return (None if (sent or {}).get("maxTokens") is None or out is None
                else int(out) >= int(sent["maxTokens"]))

    def test_the_rule(self):
        self.assertTrue(self._flag(2000, 2000))
        self.assertFalse(self._flag(1999, 2000))
        self.assertIsNone(self._flag(None, 2000), "출력 토큰을 모르면 모른다")
        self.assertIsNone(self._flag(2000, None), "전송 한도를 모르면 모른다")

    def test_the_rule_is_the_one_in_the_code(self):
        import inspect
        from assurance.coordination import agents
        source = inspect.getsource(agents)
        self.assertIn('generation["truncatedAtLimit"]', source)
        self.assertIn('int(out) >= int(limit)', source)


class TheSittingRecordsTheSection51Timestamps(unittest.TestCase):
    def test_evaluation_and_search_termination_are_stamped(self):
        from tests.test_agent_sitting import AgentSittingFixture, I1, I2
        from assurance.coordination import METHOD_THREE_AGENT
        fixture = AgentSittingFixture()
        fixture.setUp()
        try:
            sitting = fixture.build([I1, I2], method=METHOD_THREE_AGENT, budget_trials=2)
            sitting.confirm()
            sitting.run()
            record = sitting.episode_record()
            live = [t for t in record["trials"] if t.get("trialIndex") != 0]
            self.assertTrue(live, "no live trial ran")
            for trial in live:
                execution = (trial.get("kernel") or {}).get("execution") or {}
                self.assertIn("holdEndedAt", execution)
                self.assertTrue(execution.get("evaluationCompletedAt"),
                                "판정 완료 시각이 없다")
            self.assertTrue(record["termination"].get("searchTerminatedAt"),
                            "탐색 종료 시각이 없다")
        finally:
            fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
