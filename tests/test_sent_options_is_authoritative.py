"""전선에 실린 설정이 권위다 -- 요청값이 아니라 (2026-09-23 결정 §4).

`jsonMode` 와 `reasoningEffort` 는 Anthropic Messages API 에 그런 파라미터가 없어
**전송되지 않는다**.  요청값으로 표를 묶으면 캠페인이 "JSON 강제·고추론으로 돌았다" 고
읽힌다.  근거 기억: `stated-budget-is-not-the-budget-on-the-wire` (40판 전수).
"""
import unittest

from experiments.agent_metrics import generation_options_summary


NOT_SENT = {'jsonMode': "requested True, not sent: the Anthropic Messages API has no such parameter.",
            'reasoningEffort': "requested 'high', not sent: the Anthropic Messages API has no such parameter."}


def _call(**extra):
    return {'role': 'target', 'model': 'claude-sonnet', 'latencyMs': 10.0,
            'options': {'jsonMode': True, 'maxTokens': 4000, 'reasoningEffort': 'high'},
            **extra}


class TheSummaryReportsWhatWasSent(unittest.TestCase):
    def test_the_not_sent_settings_are_named(self):
        call = _call(generations=[{'sentOptions': {'maxTokens': 4000, 'notSent': dict(NOT_SENT)}}])
        row, = generation_options_summary([{'calls': [call]}])
        self.assertEqual({'maxTokens': 4000, 'notSent': NOT_SENT}, row['sentOptions'])
        self.assertEqual(NOT_SENT, row['notSent'])
        self.assertFalse(row['sentOptionsUnknown'])
        self.assertEqual(call['options'], row['requestedOptions'])
        self.assertNotIn('options', row, '요청값을 `options` 로 다시 내보내면 예전 오독이 살아난다')

    def test_a_call_with_no_sent_options_says_unknown_not_the_request(self):
        row, = generation_options_summary([{'calls': [_call(generations=[{'sentOptions': None}])]}])
        self.assertIsNone(row['sentOptions'])
        self.assertTrue(row['sentOptionsUnknown'])

    def test_two_calls_that_sent_different_things_do_not_share_a_row(self):
        rows = generation_options_summary([{'calls': [
            _call(generations=[{'sentOptions': {'maxTokens': 4000, 'notSent': dict(NOT_SENT)}}]),
            _call(generations=[{'sentOptions': {'maxTokens': 2000}}])]}])
        self.assertEqual(2, len(rows), '같은 요청값이어도 실린 것이 다르면 다른 줄이다')


if __name__ == '__main__':
    unittest.main()
