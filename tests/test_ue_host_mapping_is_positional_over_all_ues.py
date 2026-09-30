"""UE 하나의 값이 다른 UE 의 값으로 기록되면 실험이 조용히 틀린다 (2026-09-22, codex 감사).

`resolve_ue_hosts` 가 **미해결 UE 만** env 매핑에 넘겨 거기서 다시 1 부터 번호를 매겼다.
프로파일에 ue1 만 있고 ue2 가 없으면 ue2 에 `HW_UE1_HOST` -- ue1 의 기계 -- 가 붙어,
ue1 의 처리량이 ue2 의 goodput 으로 기록된다.
"""
import unittest


class EnvMappingIsPositionalOverTheWholeSet(unittest.TestCase):
    def test_a_partially_named_profile_does_not_cross_assign(self):
        from tools.liveconsole.kpi_observer import resolve_ue_hosts
        env = {'HW_UE1_HOST': 'machine-for-ue1', 'HW_UE2_HOST': 'machine-for-ue2'}
        profile = {'ueHosts': {'ue1': 'machine-for-ue1'}}
        got = resolve_ue_hosts(['ue1', 'ue2'], profile_document=profile, env=env)
        self.assertEqual('machine-for-ue1', got['ue1'])
        self.assertEqual('machine-for-ue2', got.get('ue2'),
                         'ue2 에 ue1 의 기계를 붙였다')

    def test_no_profile_still_maps_in_order(self):
        from tools.liveconsole.kpi_observer import resolve_ue_hosts
        env = {'HW_UE1_HOST': 'a', 'HW_UE2_HOST': 'b', 'HW_UE3_HOST': 'c'}
        got = resolve_ue_hosts(['ue1', 'ue2', 'ue3'], profile_document=None, env=env)
        self.assertEqual({'ue1': 'a', 'ue2': 'b', 'ue3': 'c'}, got)


if __name__ == '__main__':
    unittest.main()
