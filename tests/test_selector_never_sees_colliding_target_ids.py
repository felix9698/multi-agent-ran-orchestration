"""선택기는 T 이름과 Omega 이름을 **같은 철자로** 보면 안 된다 (2026-09-23, codex 감사 R-1).

형성된 T 는 `T1..Tn` 으로 재번호되고 Omega 는 격자 자신의 번호를 쓴다.  실측: 선택된
`T1` 은 `I1g.r1=1`, Omega 의 `T1` 은 `I3g.r1=1`.  그런데 선택기는 한 프롬프트에서
`input.target_contract`(T 이름)와 Omega 로 채점한 판정·최고·격차(Omega 이름)를 받았고,
답은 T 이름으로 검증된다.

지키는 것:
1. T 에 같은 벡터가 있는 Omega 목표는 **그 T 의 이름**으로 보인다.
2. T 에 없는 Omega 목표는 `omega:` 접두로 보인다 -- 숨기지 않되(결정 §2) T 이름과
   절대 안 겹친다.
3. T0 는 두 도메인에서 같은 벡터이므로 그대로 `T0` 다.
"""
import unittest

from assurance.coordination import (
    Authorization, OMEGA_ONLY_PREFIX, expand_targets, shown_target_ids,
)
from assurance.coordination.tc import intent_from_sentence


def _authorization():
    intents = [intent_from_sentence(sentence) for sentence in (
        "I1: UE ueId=131 needs at least 3.0 Mbps downlink, relaxable to 2.0 in 2 steps",
        "I2: UE ueId=132 needs at least 3.0 Mbps downlink, relaxable to 2.0 in 2 steps",
    )]
    return Authorization.from_intents(intents)


class TheSelectorSeesOneNamePerVector(unittest.TestCase):
    def setUp(self):
        self.omega = expand_targets(_authorization())
        omega_by_id = {t.target_id: t for t in self.omega.targets}
        # 형성된 T 를 흉내낸다: Omega 에서 둘을 골라 **T1, T2 로 재번호**한다 -- 실제
        # `from_compact` 가 하는 일이다.  고른 벡터의 Omega 번호는 T1, T2 가 아니다.
        from dataclasses import replace
        picked = [t for t in self.omega.alternatives if t.target_id not in ("T1", "T2")][:2]
        self.picked_omega_ids = [t.target_id for t in picked]
        self.formed = replace(self.omega, alternatives=tuple(
            replace(t, target_id=f"T{i + 1}") for i, t in enumerate(picked)))
        self.shown = shown_target_ids(self.omega, self.formed)
        self.omega_by_id = omega_by_id

    def test_a_vector_that_is_in_T_is_shown_by_its_T_name(self):
        for index, omega_id in enumerate(self.picked_omega_ids):
            self.assertEqual(f"T{index + 1}", self.shown[omega_id])

    def test_an_omega_T1_with_a_different_vector_is_not_shown_as_T1(self):
        """이것이 실측 충돌이다: Omega 의 T1 은 T 의 T1 과 다른 벡터다."""
        self.assertNotIn("T1", self.picked_omega_ids)
        self.assertEqual(f"{OMEGA_ONLY_PREFIX}T1", self.shown["T1"])

    def test_T0_is_the_same_vector_in_both_and_keeps_its_name(self):
        self.assertEqual("T0", self.shown["T0"])

    def test_every_shown_name_is_either_a_T_name_or_marked(self):
        t_names = {t.target_id for t in self.formed.targets}
        for omega_id, name in self.shown.items():
            self.assertTrue(name in t_names or name.startswith(OMEGA_ONLY_PREFIX),
                            f"{omega_id} -> {name}")

    def test_no_attainment_is_hidden(self):
        """결정 §2: T 밖의 달성도 숨기지 않는다 -- 모든 Omega 목표가 이름을 받는다."""
        self.assertEqual({t.target_id for t in self.omega.targets}, set(self.shown))

    def test_the_basic_monolith_whose_T_is_omega_sees_identity(self):
        self.assertEqual({t.target_id: t.target_id for t in self.omega.targets},
                         shown_target_ids(self.omega, self.omega))


if __name__ == "__main__":
    unittest.main()
