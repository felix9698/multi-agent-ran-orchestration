"""라이브 판의 귀책 분류 (2026-09-21).

오너 규칙: **에이전트가 잘못 고른 판은 결과로 남기고, 우리 코드나 장비가 망친 판은
데이터로 쓰지 않는다.**  `experiments/agent_episodes.py` 가 그 규칙을 이미 구현했지만
라이브 판은 그 모듈을 지나지 않아 `failures` 가 한 번도 채워진 적이 없었다 — 2026-09-21
전수 집계에서 321판 중 0판이었고, 그래서 원복 오탐·두 시계로 죽은 판이 전부 D 통계에
섞여 있었다.

증거가 없으면 분류하지 않는다: 의심만으로 판을 버리면 불리한 결과를 조용히 지우는 것과
구별되지 않는다.
"""
import unittest

from tools.liveconsole.agent import attribute_failures


class CleanBoardsStayData(unittest.TestCase):
    def test_a_board_with_no_evidence_of_our_fault_is_kept(self):
        self.assertEqual(attribute_failures(
            {"hardwareDisconnects": [], "kpiObserverFailures": []}), [])

    def test_an_empty_document_is_kept(self):
        """필드가 아예 없는 판을 버리면 안 된다 — 모르는 것은 우리 잘못이 아니다."""
        self.assertEqual(attribute_failures({}), [])

    def test_a_method_that_simply_failed_is_kept(self):
        """에이전트가 아무것도 달성하지 못한 판은 **결과**다."""
        self.assertEqual(attribute_failures({
            "termination": {"kernelTermination": "SUCCESS",
                            "reason": "CATALOG_EXHAUSTED"},
            "bestAttained": None}), [])


class ExternalFaultsAreMarked(unittest.TestCase):
    def _kinds(self, document):
        return [item["kind"] for item in attribute_failures(document)]

    def test_hardware_disconnects_are_excised_not_attributed(self):
        """2026-09-23 오너 ("볼드모트"): 끊김은 판을 버리는 사유가 아니다 -- 구간을 도려내고 이어간다."""
        self.assertEqual([], attribute_failures({"hardwareDisconnects": [{"at": "t"}, {"at": "u"}]}))

    def test_a_dead_observer_is_excised_not_attributed(self):
        """telemetry 결손은 시행의 KPI 실패도, 판 제외 사유도 아니다 -- 도려낸다."""
        self.assertEqual(self._kinds({"kpiObserverFailures": [{"error": "clock"}]}), [])

    def test_an_interruption_past_its_bound_is_attributed(self):
        found = attribute_failures({
            "termination": {"reason": "HARDWARE_UNAVAILABLE", "detail": "waited 300 s"},
            "excision": {"totalMs": 610000, "capMs": 600000, "intervals": [[0, 610000]]}})
        self.assertEqual(["external-equipment-failure"], [f["kind"] for f in found])
        self.assertFalse(found[0]["methodCaused"])
        self.assertEqual(610000, found[0]["evidence"]["excisedTotalMs"])

    def test_the_two_clocks_defect(self):
        self.assertEqual(self._kinds({"completion": {"unresolved": [
            {"detail": "the case refused: CASE_NOT_TERMINABLE"}]}}),
            ["harness-defect"])

    def test_a_reversal_mismatch_is_never_our_defect(self):
        """원복 실패는 **진짜 안전 사건**이다 — 우리 결함으로 분류해 판을 버리면 안 된다.

        2026-09-21 에 나는 `observed == permit expected` 를 "원복은 됐는데 비교가 틀렸다"
        로 읽고 이 자리에 제외 규칙을 넣었다. 틀렸다: `expected_config_hash` 는 게이트웨이가
        **행동하기 전에** 관측해야 하는 값이라, 원복 뒤 그것과 같다는 건 아무것도 안
        움직였다는 뜻이다. 규칙을 남겼으면 진짜 안전 사건을 조용히 지웠을 것이다.
        """
        for observed in ("62dca97760e0", "aaaaaaaaaaaa"):
            self.assertEqual(attribute_failures({"trials": [{"detail":
                "reversal did not restore the baseline configuration "
                f"(observed {observed}, transaction baseline b9059c4baba0, "
                "permit expected 62dca97760e0)"}]}), [],
                "원복 판정은 게이트웨이의 몫이지 귀책 분류의 몫이 아니다")

    def test_several_faults_are_all_listed(self):
        self.assertEqual(sorted(self._kinds({
            "hardwareDisconnects": [{"at": "t"}],
            "kpiObserverFailures": [{"error": "e"}],
            "termination": {"reason": "HARDWARE_UNAVAILABLE", "detail": "cap"},
            "completion": {"unresolved": [{"detail": "CASE_NOT_TERMINABLE"}]}})),
            ["external-equipment-failure", "harness-defect"])


class AMismatchIsNotAnEquipmentFailure(unittest.TestCase):
    """불일치 탐지를 외부 장애로 적으면 불리한 판이 세탁된다 (2026-09-22).

    `kpiObserverFailures` 를 통째로 "장비가 끊겼다" 로 분류하고 있었다.  그런데
    `flow-payload-mismatch` 는 끊김이 아니라 "tun 은 받는데 우리 flow 의 payload 만
    안 늘었다" 는 **사실**뿐이다 -- 낡은 주소일 수도 있고, **고른 제어가 그 flow 를
    굶긴 것**일 수도 있다.  원인을 증명하지 못했으면 분류하지 않고 데이터로 남긴다.
    """

    MISMATCH = {"ueId": "ue3", "kind": "flow-payload-mismatch",
                "error": "flow payload did not advance while the tun received +99999 B"}
    DISCONNECT = {"ueId": "ue1", "host": "ue1", "error": "SSHError: connection closed"}

    def test_a_mismatch_alone_is_not_attributed(self):
        self.assertEqual([], attribute_failures(
            {"kpiObserverFailures": [self.MISMATCH]}))

    def test_a_real_disconnect_is_excised_too(self):
        self.assertEqual([], attribute_failures({"kpiObserverFailures": [self.DISCONNECT]}))

    def test_a_mismatch_beside_a_disconnect_still_attributes_nothing(self):
        self.assertEqual([], attribute_failures(
            {"kpiObserverFailures": [self.MISMATCH, self.DISCONNECT]}))

if __name__ == "__main__":
    unittest.main()
