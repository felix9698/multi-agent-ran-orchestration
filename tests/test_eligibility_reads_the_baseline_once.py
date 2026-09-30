"""후보 적격 판정이 카탈로그 크기만큼 게이트웨이를 조회하면 판이 계산에 갇힌다 (2026-09-22).

실시간 계측: 시팅이 `aiming at T0 x C1` 출력 뒤 코어 하나를 100% 로 태우며 5분 넘게
아무것도 쓰지 않았다(누적 CPU 41초 -> 5분 31초, stdout 341초 무갱신).  정체는
`_eligible_remaining()` 이 control 마다 `_withdrawal_blocked()` 를 부르고 그 안에서
매번 `applied_configuration()` -> `live_baseline()` 을 조회한 것이다.  이 판의 카탈로그는
512개였다.  여기서 지키는 것은 하나: **카탈로그 전체에 조회 한 번.**
"""
import unittest
from types import SimpleNamespace


class EligibilityReadsTheLiveBaselineOnce(unittest.TestCase):
    def test_one_read_no_matter_how_many_controls(self):
        from tools.liveconsole.agent import AgentSitting

        sitting = AgentSitting.__new__(AgentSitting)
        reads = []

        def applied_configuration():
            reads.append(1)
            return {}

        sitting.applied_configuration = applied_configuration
        sitting.catalog_of_control = {'C%d' % i: 'cand%d' % i for i in range(200)}
        sitting.controls = SimpleNamespace(candidate=lambda _cid: None)
        sitting._method = 'three-agent'
        sitting._spent_elsewhere = lambda: set()

        try:
            sitting._eligible_remaining()
        except Exception:
            # 뒤쪽 집계는 이 시험의 관심사가 아니다 -- 조회 횟수만 본다.
            pass

        self.assertEqual(1, len(reads),
                         'control %d개에 대해 live_baseline 을 %d번 읽었다'
                         % (len(sitting.catalog_of_control), len(reads)))


if __name__ == '__main__':
    unittest.main()
