"""Kernel 의 case 마감은 에피소드 예산 B 와 같은 시계여야 한다 (2026-09-21).

실측: `compose_joint` 에 `deadline_ms` 를 안 넘기면 case 마감을
`trials*(hold_ms+180_000)+600_000` 으로 스스로 만든다. 판 하나에서 에피소드 B 는
480,000 ms(8분)인데 case 마감은 8*(21,000+180,000)+600,000 = 2,208,000 ms(36.8분)였다.
그러면 에피소드가 자기 마감으로 멈출 때 Kernel 이 `CASE_NOT_TERMINABLE` 로 거절하고
종료상태 칸이 빈다 — 35판이 그렇게 기록을 잃었고 그중 6판은 인텐트를 전부 충족했다.
"""
import unittest

from assurance.objectives.joint import compose_joint
from tests.assurance.test_joint_objective import (  # type: ignore
    HOME, TARGET, deployment, two_intents)
from assurance.objectives.joint import SteeringAxisSpec


class CaseDeadlineFollowsTheEpisodeBudget(unittest.TestCase):
    def _policy(self, **kwargs):
        intents = two_intents()
        ues = tuple(dict.fromkeys(item.ue_id for item in intents))
        joint = compose_joint(
            intents=intents, deployment_binding=deployment(),
            steering_axes=tuple(SteeringAxisSpec(ue, HOME, (HOME, TARGET)) for ue in ues),
            budget_trials=8, hold_ms=21_000, **kwargs)
        return joint.bundle.case_policy

    def test_the_derived_deadline_is_far_longer_than_b(self):
        """넘기지 않으면 폐형식으로 떨어진다 — 이 값이 B 보다 크다는 것이 결함의 전제다."""
        derived = self._policy().deadline_ms
        self.assertEqual(derived, 8 * (21_000 + 180_000) + 600_000)
        self.assertGreater(derived, 480_000)

    def test_passing_b_makes_the_two_clocks_one(self):
        self.assertEqual(self._policy(deadline_ms=480_000).deadline_ms, 480_000)


if __name__ == "__main__":
    unittest.main()
