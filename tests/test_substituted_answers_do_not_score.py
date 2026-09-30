"""결정론 답으로 대체된 판은 그 방식의 D 를 낼 수 없다 (2026-09-21).

대체가 일어나면 그 뒤 시행은 방식이 고른 것이 아니라 코드가 고른 것을 집행한다.
그 판의 양보는 방식의 양보가 아니다.

**그렇다고 버려서도 안 된다.**  2026-09-21 실측(162판) 29판의 대체 사유는 "답이 두 번
연속 관측 유효기간을 넘겨 도착"(26건)과 "인가되지 않은 완화 제안"(3건)으로 **둘 다 방식
귀책**이고, `orc_task/exp_metrics.md` 5절은 방법 자체의 오류를 결과에 포함하라고 한다.
그래서 분모(N)에는 남기고 D(n)에서만 뺀다 -- 고치기 전에는 29판 중 8판이 성공으로 셌다.
"""

from __future__ import annotations

import unittest

from experiments.agent_metrics import concession_summary, substituted_answer


def board(*, fallback=None):
    episode = {
        'episodeId': 'E' + ('f' if fallback else 'ok'),
        'calls': [{'role': 'target', 'fallbackReason': fallback}],
        'intents': [{'intentId': 'I1', 'owner': 'ue1',
                     'requirement': {'reqId': 'I1.r1', 'kpi': 'dlGoodputMbps',
                                     'scope': 'ue@ue1', 'op': '>=', 'value': 9.0,
                                     'unit': 'Mbps', 'bound': 5.6, 'relaxable': True}}],
        'T': {'authorization': {},
              't0': {'targetId': 'T0', 'requirements': {'I1.r1': 9.0}},
              'alternatives': [{'targetId': 'T1', 'requirements': {'I1.r1': 5.6}}]},
        'trials': [{'trialIndex': 0, 'counted': True, 'success': True,
                    'controlId': 'C1', 'proposedTargetId': 'T1',
                    'verdicts': {'T1': {'I1.r1': 'PASS'}},
                    'elapsedMs': 1000.0, 'rolledBack': False}],
        'bestAttained': {'targetId': 'T1', 'controlId': 'C1', 'trialIndex': 0},
    }
    return episode


class ASubstitutedAnswerIsNotTheMethodsConcession(unittest.TestCase):
    def test_it_is_named_with_its_role_and_reason(self):
        self.assertIsNone(substituted_answer(board()))
        named = substituted_answer(board(fallback='two answers arrived stale'))
        self.assertIn('target', named)
        self.assertIn('stale', named)

    def test_it_stays_in_the_denominator_but_yields_no_d(self):
        rows = concession_summary([board(fallback='two answers arrived stale')])
        best = rows['bestAttained']
        self.assertEqual(1, best['N'], '분모에서 빠졌다 -- 방식 귀책 실패가 안 보인다')
        self.assertEqual(0, best['n'], '대체된 답이 D 를 냈다')
        self.assertEqual(1, best['substitutedAnswerCount'])
        self.assertEqual(0, best['excludedCount'], '제외 규칙으로 버리면 안 된다')

    def test_a_clean_board_is_unaffected(self):
        best = concession_summary([board()])['bestAttained']
        self.assertEqual(1, best['N'])
        self.assertEqual(0, best['substitutedAnswerCount'])


if __name__ == '__main__':
    unittest.main()
