"""Corroborated readback: two independent observations, or ``None``."""

from __future__ import annotations

import struct
import unittest

from oran.campaign5.families import CAMPAIGN5_FAMILIES
from oran.campaign5.readback import (
    AbsentCounterReader,
    CorroboratedConfigReadback,
    DictKpmConfigReader,
    make_status_projection,
)

FAM = CAMPAIGN5_FAMILIES["cap"]
SCOPE = {"cellId": "cell-1", "ueId": "ue-1"}
CONFIG = {"cellId": "cell-1", "ueId": "ue-1", "maxDlPrbs": 12}


class StubStatusPort:
    def __init__(self, status):
        self.status = status

    def get_policy_status(self, policy_id):
        del policy_id
        return self.status


def verified_status(observed):
    return {"aicStatus": {"episodeState": "APPLIED_VERIFIED", "episodeTerminal": True,
                          "readback": {"result": "VERIFIED", FAM.observed_key: observed}}}


def unverified_status():
    return {"aicStatus": {"episodeState": "APPLIED_UNVERIFIED", "episodeTerminal": False,
                          "readback": {"result": "NOT_AVAILABLE"}}}


def readback(status_port, kpm, deadline_ms=30):
    ticks = [0]

    def monotonic():
        ticks[0] += 10
        return ticks[0]

    return CorroboratedConfigReadback(
        FAM, status_port=status_port, kpm_reader=kpm,
        monotonic_ms=monotonic, sleep_ms=lambda _ms: None,
        cadence_ms=10, deadline_ms=deadline_ms,
    )


class Corroboration(unittest.TestCase):
    def test_agreement_of_producer_and_counter_yields_the_config_on_axis(self):
        kpm = DictKpmConfigReader()
        kpm.publish(FAM.readback_counter, SCOPE, {"maxDlPrbs": 12})
        rb = readback(StubStatusPort(verified_status(CONFIG)), kpm)
        observed = rb(scope=SCOPE, transaction_id="tx-1", policy_id="pol-1")
        # Returned on the plan's axis surface, like steering's {"servingCell": nci}.
        self.assertEqual(observed, {FAM.axis: {"maxDlPrbs": 12}})

    def test_producer_verified_but_counter_absent_is_not_an_effect(self):
        rb = readback(StubStatusPort(verified_status(CONFIG)), AbsentCounterReader())
        self.assertIsNone(rb(scope=SCOPE, transaction_id="tx-1", policy_id="pol-1"))

    def test_producer_verified_but_counter_disagrees_is_not_an_effect(self):
        kpm = DictKpmConfigReader()
        kpm.publish(FAM.readback_counter, SCOPE, {"maxDlPrbs": 99})
        rb = readback(StubStatusPort(verified_status(CONFIG)), kpm)
        self.assertIsNone(rb(scope=SCOPE, transaction_id="tx-1", policy_id="pol-1"))

    def test_an_ack_without_verified_readback_times_out_to_none(self):
        kpm = DictKpmConfigReader()
        kpm.publish(FAM.readback_counter, SCOPE, {"maxDlPrbs": 12})
        rb = readback(StubStatusPort(unverified_status()), kpm)
        self.assertIsNone(rb(scope=SCOPE, transaction_id="tx-1", policy_id="pol-1"))

    def test_pre_commit_baseline_reads_from_the_counter_only(self):
        kpm = DictKpmConfigReader()
        kpm.publish(FAM.readback_counter, SCOPE, {"maxDlPrbs": 24})
        rb = readback(StubStatusPort(verified_status(CONFIG)), kpm)
        observed = rb(scope=SCOPE, transaction_id="tx-1", policy_id=None)
        self.assertEqual(observed, {FAM.axis: {"maxDlPrbs": 24}})

    def test_pre_commit_baseline_is_none_when_the_counter_is_absent(self):
        rb = readback(StubStatusPort(verified_status(CONFIG)), AbsentCounterReader())
        self.assertIsNone(rb(scope=SCOPE, transaction_id="tx-1", policy_id=None))


class LateCounterReader:
    """A live KPM reader: the counter is published once per period, so the
    first reads between two indications answer ``None`` even though the counter
    is on the stream."""

    def __init__(self, inner, absent_reads):
        self.inner = inner
        self.absent_reads = absent_reads
        self.reads = 0

    def read(self, counter, scope):
        self.reads += 1
        if self.reads <= self.absent_reads:
            return None
        return self.inner.read(counter, scope)


def readback_counting_sleeps(status_port, kpm, deadline_ms=100):
    ticks = [0]
    sleeps = []

    def monotonic():
        ticks[0] += 10
        return ticks[0]

    rb = CorroboratedConfigReadback(
        FAM, status_port=status_port, kpm_reader=kpm,
        monotonic_ms=monotonic, sleep_ms=sleeps.append,
        cadence_ms=10, deadline_ms=deadline_ms,
    )
    return rb, sleeps


class WaitsForTheNextIndication(unittest.TestCase):
    """Observed over the air 2026-09-06: a single read between two KPM periods
    reported COUNTER_ABSENT for a UE whose counter was on the stream one second
    later, and the composition failed before any policy existed."""

    def test_pre_commit_baseline_polls_until_the_counter_is_published(self):
        kpm = DictKpmConfigReader()
        kpm.publish(FAM.readback_counter, SCOPE, {"maxDlPrbs": 24})
        late = LateCounterReader(kpm, absent_reads=2)
        rb, sleeps = readback_counting_sleeps(StubStatusPort(verified_status(CONFIG)), late)
        observed = rb(scope=SCOPE, transaction_id="tx-1", policy_id=None)
        self.assertEqual(observed, {FAM.axis: {"maxDlPrbs": 24}})
        self.assertEqual(late.reads, 3)
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(all(0 < ms <= 10 for ms in sleeps))

    def test_pre_commit_baseline_is_still_none_past_the_deadline(self):
        late = LateCounterReader(AbsentCounterReader(), absent_reads=10 ** 6)
        rb, sleeps = readback_counting_sleeps(
            StubStatusPort(verified_status(CONFIG)), late, deadline_ms=50)
        self.assertIsNone(rb(scope=SCOPE, transaction_id="tx-1", policy_id=None))
        self.assertGreaterEqual(len(sleeps), 1)
        self.assertLessEqual(late.reads, 8)

    def test_post_commit_waits_for_the_counter_when_the_producer_is_verified(self):
        kpm = DictKpmConfigReader()
        kpm.publish(FAM.readback_counter, SCOPE, {"maxDlPrbs": 12})
        late = LateCounterReader(kpm, absent_reads=2)
        rb, sleeps = readback_counting_sleeps(StubStatusPort(verified_status(CONFIG)), late)
        observed = rb(scope=SCOPE, transaction_id="tx-1", policy_id="pol-1")
        self.assertEqual(observed, {FAM.axis: {"maxDlPrbs": 12}})
        self.assertEqual(late.reads, 3)
        self.assertEqual(len(sleeps), 2)

    def test_post_commit_disagreement_is_retried_until_the_deadline(self):
        """2026-09-18 계약 변경.  이 테스트는 원래 '커밋 뒤 불일치는 즉시, 재시도 없음'
        이었다.  라이브가 그 계약을 반증했다: KPM 은 주기로 발행하므로 커밋 직후의 첫
        지시는 **아직 안 바뀐 옛 값**을 싣는다.  그것을 불일치로 읽으면 전력 축 시행이
        전부 PARTIAL_APPLY samples 0 으로 닫힌다 — 15 판 4 시행이 그랬고, 라디오는
        몇 초 뒤 실제로 바뀌어 있었다 (APublishedButStaleCounterIsNotADisagreement 참조).

        그래서 이제는 마감까지 다시 읽는다.  **진짜 불일치도 여전히 실패로 끝나되**,
        즉시가 아니라 마감을 넘긴 뒤다.  마감이 그 기다림을 유계로 만든다.
        """
        kpm = DictKpmConfigReader()
        kpm.publish(FAM.readback_counter, SCOPE, {"maxDlPrbs": 99})
        rb, sleeps = readback_counting_sleeps(StubStatusPort(verified_status(CONFIG)), kpm)
        self.assertIsNone(rb(scope=SCOPE, transaction_id="tx-1", policy_id="pol-1"))
        self.assertTrue(sleeps, "끝내 다른 값이어도, 갱신을 기다려는 봐야 한다")
        self.assertTrue(all(0 < ms <= 10 for ms in sleeps), sleeps)
        self.assertLess(len(sleeps), 20, "마감이 기다림을 유계로 만들어야 한다")

    def test_post_commit_counter_never_published_is_none_past_the_deadline(self):
        late = LateCounterReader(AbsentCounterReader(), absent_reads=10 ** 6)
        rb, sleeps = readback_counting_sleeps(
            StubStatusPort(verified_status(CONFIG)), late, deadline_ms=50)
        self.assertIsNone(rb(scope=SCOPE, transaction_id="tx-1", policy_id="pol-1"))
        self.assertGreaterEqual(len(sleeps), 1)


class VerifiedOnlyProjection(unittest.TestCase):
    def test_projection_rejects_a_non_verified_readback(self):
        project = make_status_projection(FAM)
        self.assertIsNone(project({"aicStatus": {"readback": {"result": "NOT_AVAILABLE"}}}))
        self.assertIsNone(project({"aicStatus": {"readback": {"result": "MISSING"}}}))
        self.assertIsNone(project({"aicStatus": {}}))

    def test_projection_returns_the_observed_config_only_when_verified(self):
        project = make_status_projection(FAM)
        self.assertEqual(project(verified_status(CONFIG)), CONFIG)


class FloatCorroboration(unittest.TestCase):
    def test_float32_round_trip_verifies_but_a_material_difference_mismatches(self):
        # The gNB stores each of these leaves as C float, while KPM parses the
        # emitted real number as Python float (double precision).
        cases = (
            (CAMPAIGN5_FAMILIES["priority"], {"cellId": "cell-1", "ueId": "ue-1"},
             {"pfWeight": 3.7}, "pfWeight"),
            (CAMPAIGN5_FAMILIES["power"], {"cellId": "cell-1", "gnbId": "gnb-1"},
             {"txAttenuationDb": 23.7}, "txAttenuationDb"),
        )
        for family, scope, leaves, field in cases:
            with self.subTest(family=family.key):
                float32_value = struct.unpack("!f", struct.pack("!f", leaves[field]))[0]
                self.assertNotEqual(float32_value, leaves[field])
                config = {**scope, **leaves}
                status = {"aicStatus": {"episodeState": "APPLIED_VERIFIED",
                          "episodeTerminal": True,
                          "readback": {"result": "VERIFIED",
                                       family.observed_key: config}}}
                kpm = DictKpmConfigReader()
                kpm.publish(family.readback_counter, scope, {field: float32_value})
                observed = CorroboratedConfigReadback(
                    family, status_port=StubStatusPort(status), kpm_reader=kpm,
                    monotonic_ms=lambda: 0, sleep_ms=lambda _ms: None,
                    cadence_ms=10, deadline_ms=10,
                )(scope=scope, transaction_id="tx-1", policy_id="pol-1")
                self.assertEqual(observed, {family.axis: leaves})

                kpm.publish(family.readback_counter, scope, {field: leaves[field] + 0.01})
                # 2026-09-19: 되읽기가 '옛 값이면 마감까지 기다린다' 로 바뀌어, 멈춘 시계
                # (lambda: 0)로는 마감에 닿지 못해 이 테스트가 끝나지 않았다(커널 스위트가
                # 50 분을 넘긴 원인).  잠들 때마다 흐르는 가짜 시계를 쓴다.
                clock = {"ms": 0}
                mismatch = CorroboratedConfigReadback(
                    family, status_port=StubStatusPort(status), kpm_reader=kpm,
                    monotonic_ms=lambda: clock["ms"],
                    sleep_ms=lambda ms: clock.__setitem__("ms", clock["ms"] + max(1, ms)),
                    cadence_ms=10, deadline_ms=10,
                )(scope=scope, transaction_id="tx-1", policy_id="pol-1")
                self.assertIsNone(mismatch)


if __name__ == "__main__":
    unittest.main()

class StaleCounterReader:
    """A live KPM reader whose counter is published throughout but keeps the
    OLD value for the first few reads -- an RF attenuation takes seconds to
    land, and the counter says so honestly while it does."""

    def __init__(self, inner, stale_value, stale_reads):
        self.inner = inner
        self.stale_value = stale_value
        self.stale_reads = stale_reads
        self.reads = 0

    def read(self, counter, scope):
        self.reads += 1
        if self.reads <= self.stale_reads:
            return dict(self.stale_value)
        return self.inner.read(counter, scope)


class APublishedButStaleCounterIsNotADisagreement(unittest.TestCase):
    """실측 2026-09-18: 감쇠 쓰기 뒤 `RAN.Cell.TxAttenuationDb` 가 움직이기까지
    10.5 초와 12.2 초 걸렸다(판 20260918T104544 · T103405).  그때까지 카운터는
    **발행되지만 옛 값**을 싣는데, 되읽기가 그 첫 불일치에서 즉시 None 을 냈다.
    두 판 모두 라디오가 몇 초 뒤 실제로 3.0 dB 로 바뀌었는데도 PARTIAL_APPLY 로
    닫혔다 — 전력 축이 한 번도 SETTLED_SUCCESS 를 못 낸 이유다.
    """

    def test_the_readback_waits_out_a_stale_value_instead_of_failing_at_once(self):
        kpm = DictKpmConfigReader()
        kpm.publish(FAM.readback_counter, SCOPE, {"maxDlPrbs": 12})
        stale = StaleCounterReader(kpm, {"maxDlPrbs": 0}, stale_reads=2)
        rb, sleeps = readback_counting_sleeps(
            StubStatusPort(verified_status(CONFIG)), stale)
        observed = rb(scope=SCOPE, transaction_id="tx-1", policy_id="pol-1")
        self.assertEqual(observed, {FAM.axis: {"maxDlPrbs": 12}})
        self.assertEqual(stale.reads, 3)          # 옛 값 둘을 지나 셋째에 일치
        self.assertEqual(len(sleeps), 2)

    def test_a_disagreement_that_survives_the_deadline_is_still_none(self):
        kpm = DictKpmConfigReader()
        kpm.publish(FAM.readback_counter, SCOPE, {"maxDlPrbs": 12})
        never = StaleCounterReader(kpm, {"maxDlPrbs": 0}, stale_reads=10**6)
        rb, _ = readback_counting_sleeps(
            StubStatusPort(verified_status(CONFIG)), never, deadline_ms=100)
        self.assertIsNone(
            rb(scope=SCOPE, transaction_id="tx-1", policy_id="pol-1"))

