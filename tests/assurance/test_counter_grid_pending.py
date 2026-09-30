"""A grid collector keeps only samples that can still feed a slot.

2026-09-15 OTA attempt 44: the KPM tail starts at offset 0, so each collector
received the whole history of every UE; nothing unused was ever dropped and each
poll re-sorted and rescanned it (~340 ms per counter, 3 s per poll, STALE).
"""

import unittest
from datetime import timedelta

from assurance.collector.samples import ClockHealth, RawSample
from assurance.contracts.measurement import MeasurementSource
from assurance.core.addressing import content_hash
from assurance.core.provenance import Provenance, TypedQuantity
from assurance.core.timebase import format_utc, parse_utc
from assurance.live.objective_runtime import CounterGeometry, _CounterGridCollector

ANCHOR = parse_utc("2026-09-15T00:10:00Z")


def kpm(ue, seconds, value):
    at = format_utc(ANCHOR + timedelta(seconds=seconds, milliseconds=-400))
    sid = content_hash({"ue": ue, "at": at})
    return RawSample(
        sample_id=sid, counter_id="RAN.UE.DlPrbCap",
        value=TypedQuantity(value, "1", Provenance.MEASURED, sid),
        scope_snapshot={"amf_ue_ngap_id": ue}, observed_at=at, cadence_ms=1000,
        clock_health=ClockHealth.SYNCHRONISED, trace_hash=sid, sequence=0)


class OnlyUsableSamplesArePending(unittest.TestCase):
    def test_history_and_other_ues_are_dropped_and_slots_keep_their_values(self):
        history = [kpm(ue, s, 99.0) for s in range(-3600, -1) for ue in ("2227", "2234")]
        batches = [history + [kpm("2227", 0, 18.0), kpm("2234", 0, 7.0)]]
        batches += [[kpm("2227", s, 18.0 + s), kpm("2234", s, 7.0)] for s in range(1, 6)]
        geometry = CounterGeometry(
            counter_id="counter/joint@2227/ue-dl-prb-cap",
            deployment_counter_name="RAN.UE.DlPrbCap",
            source=MeasurementSource.E2_KPM, scope={"controlledUeId": "2227"},
            measurement_refs=(), cadence_ms=1000, window_width_ms=1000,
            hold_ms=5000, freshness_bound_ms=1000)
        collector = _CounterGridCollector(
            geometry=geometry, anchor=lambda: format_utc(ANCHOR),
            load_samples=lambda: batches.pop(0) if batches else (), membership=())
        values = []
        for s in range(0, 6):
            produced = collector.poll(now=format_utc(ANCHOR + timedelta(seconds=s)))
            values += [sample.value.value for sample in produced]
            self.assertLessEqual(len(collector._pending), 2)
        self.assertEqual(values, [18.0, 19.0, 20.0, 21.0, 22.0, 23.0])


class TheGridFollowsAUeThatReRegisters(unittest.TestCase):
    """형제 자리를 전부 고쳐라 (2026-09-22).

    `ServingCellCollector` 는 2026-09-17 에 `_resolved_or_pinned` 를 받아 재등록을
    따라가게 고쳐졌는데, 바로 옆에서 만들어지는 counter grid 의 `ue_aliases` 만
    조립 시점 번호로 얼어 있었다.  UE 가 61 -> 68 로 재등록하면 기록은 68 로
    들어오는데 기대값은 61 에 남아 `_scope_matches` 가 **그 UE 의 기록을 전부**
    거절한다.  그러면 그 counter 의 coverage 가 미달해 KPI 가 통째로 게시되지 않는다.
    """

    def geometry(self):
        return CounterGeometry(
            counter_id="counter/joint@ue1/ue-dl-prb-cap",
            deployment_counter_name="RAN.UE.DlPrbCap",
            source=MeasurementSource.E2_KPM, scope={"controlledUeId": "ue1"},
            measurement_refs=(), cadence_ms=1000, window_width_ms=1000,
            hold_ms=5000, freshness_bound_ms=1000)

    def collect(self, alias):
        batches = [[kpm("68", s, 10.0 + s)] for s in range(0, 4)]
        collector = _CounterGridCollector(
            geometry=self.geometry(), anchor=lambda: format_utc(ANCHOR),
            load_samples=lambda: batches.pop(0) if batches else (),
            membership=(), ue_aliases={"ue1": alias})
        produced = []
        for s in range(0, 4):
            produced += collector.poll(now=format_utc(ANCHOR + timedelta(seconds=s)))
        return produced

    def test_a_frozen_alias_turns_every_slot_into_a_gap(self):
        produced = self.collect("61")
        self.assertTrue(produced, "칸 자체는 채워진다")
        self.assertTrue(all(sample.missing_intervals for sample in produced),
                        "얼린 번호는 재등록한 UE 의 기록을 하나도 못 받는다")

    def test_a_resolver_alias_keeps_collecting_across_the_re_registration(self):
        produced = self.collect(lambda: 68)
        self.assertEqual([10.0, 11.0, 12.0, 13.0],
                         [sample.value.value for sample in produced])
        self.assertFalse(any(sample.missing_intervals for sample in produced),
                         "해석기를 주면 새 번호의 기록이 그대로 수집된다")


if __name__ == "__main__":
    unittest.main()
