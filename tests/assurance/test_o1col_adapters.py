"""Gate 3 source-adapter behavior over synthetic O1 and KPM evidence."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from assurance.collector.collector import MeasurementCollector
from assurance.collector.o1col import (
    KpmJsonlAdapter,
    O1PmFileAdapter,
    correlate_after_window,
)
from assurance.collector.samples import ClockHealth, RawSample
from assurance.contracts.measurement import (
    Aggregation, ClockRequirement, Estimator, GapPolicy, MeasurementContract,
    OverlapPolicy, UncertaintyRule,
)
from assurance.core.provenance import Provenance, TypedQuantity


FIXTURES = Path(__file__).with_name("fixtures")


def _contract() -> MeasurementContract:
    return MeasurementContract(
        contract_id="m-1", version="1.0.0", schema_version="assurance/1.0.0",
        document_status="NORMATIVE", standard_mapping={}, counter_id="RRU.PrbDl",
        scope_selector={}, membership_snapshot=(), cadence_ms=60_000,
        window_width_ms=60_000, window_stride_ms=60_000,
        overlap=OverlapPolicy.DISJOINT, aggregation=Aggregation.MEAN,
        estimator=Estimator.SAMPLE_MEAN, minimum_entity_count=1, hold_ms=0,
        gap_policy=GapPolicy.REJECT_WINDOW,
        missing_interval_charge=TypedQuantity(1, "1", Provenance.EXPERIMENT_CONFIG, "gap"),
        freshness_bound_ms=60_000, clock_requirement=ClockRequirement.SYNCHRONISED_REQUIRED,
        uncertainty_rule=UncertaintyRule("bounded_absolute", TypedQuantity(0, "1", Provenance.EXPERIMENT_CONFIG, "uncertainty")),
    )


class O1PmAdapterTests(unittest.TestCase):
    def test_parses_two_cell_prb_samples_without_retimestamping(self):
        adapter = O1PmFileAdapter(read_bytes=lambda _: (FIXTURES / "o1col-pm.xml").read_bytes())
        samples = adapter.collect(["synthetic.xml"])
        self.assertEqual([sample.value.value for sample in samples], [41, 67])
        self.assertEqual([sample.scope_snapshot["nrCellDu"] for sample in samples], ["1", "2"])
        self.assertEqual(samples[0].observed_at, "2026-08-21T09:01:00.000000Z")
        self.assertEqual(samples[0].cadence_ms, 60_000)
        self.assertEqual(samples[0].clock_health, ClockHealth.UNKNOWN)
        self.assertEqual(len(samples[0].trace_hash), 64)
        self.assertTrue(all(isinstance(sample, RawSample) for sample in samples))
        self.assertTrue(isinstance(adapter, MeasurementCollector))

    def test_nil_and_suspect_remain_explicit_missing_unhealthy_evidence(self):
        adapter = O1PmFileAdapter(read_bytes=lambda _: (FIXTURES / "o1col-pm-suspect.xml").read_bytes())
        (sample,) = adapter.collect(["suspect.xml"])
        self.assertEqual(sample.value.value, 0)
        self.assertTrue(sample.has_gaps())
        self.assertEqual(sample.clock_health, ClockHealth.DRIFTING_OUT_OF_BOUND)


class KpmAdapterTests(unittest.TestCase):
    def test_keeps_format1_and_format3_and_fail_closes_bad_epochs_and_lines(self):
        result = KpmJsonlAdapter(expected_epochs={"gNB1": 161}).parse_lines(
            (FIXTURES / "o1col-kpm.jsonl").read_text().splitlines()
        )
        self.assertEqual(len(result.samples), 2)
        self.assertEqual(result.samples[0].counter_id, "RRC.ConnMean")
        self.assertEqual(result.samples[0].scope_snapshot["slot"], "0")
        self.assertEqual(result.samples[1].scope_snapshot["amf_ue_ngap_id"], "130")
        # 2026-09-21: 이 둘은 **다른 사건**이고 조치도 다르다.  예전에는
        # `missing_records` 가 `invalid_records` 의 문자 그대로의 복사본이라 둘 다 2 였다 --
        # "JSON 한 줄이 깨졌다"(무해)와 "gNB 재기동으로 epoch 이 올라 그 노드의 실측이
        # 통째로 사라졌다"(치명, Kernel 에는 MissingInterval 조차 안 간다)를 한 숫자로
        # 뭉갠 것이다.  픽스처의 거부 2줄은 epoch 999 한 줄과 깨진 JSON 한 줄이다.
        self.assertEqual(result.invalid_records, 1)   # 깨진 JSON
        self.assertEqual(result.missing_records, 1)   # epoch 불일치로 거부된 멀쩡한 실측

    def test_configuration_readback_counters_have_typed_units(self):
        record = {
            "event": "kpm_indication", "e2_node": "gNB1", "connection_epoch": 161,
            "recv_unix_us": 1_788_000_000_000_000,
            "measurements": [
                {"name": "RAN.UE.DlPrbCap", "type": "int", "value": 12},
                {"name": "RAN.UE.PfWeight", "type": "real", "value": 1.5},
                {"name": "RAN.Cell.DlMcsBounds", "type": "int", "value": 16},
                {"name": "RAN.Cell.TxAttenuationDb", "type": "real", "value": 23.5},
            ],
        }
        result = KpmJsonlAdapter(expected_epochs={"gNB1": 161}).parse_lines([json.dumps(record)])
        self.assertEqual(
            {sample.counter_id: sample.value.unit for sample in result.samples},
            {
                "RAN.UE.DlPrbCap": "PRB",
                "RAN.UE.PfWeight": "ratio",
                "RAN.Cell.DlMcsBounds": "MCS-index",
                "RAN.Cell.TxAttenuationDb": "dB",
            },
        )


class AfterWindowTests(unittest.TestCase):
    def test_after_window_membership_and_completion_matrix(self):
        adapter = O1PmFileAdapter(read_bytes=lambda _: (FIXTURES / "o1col-pm.xml").read_bytes())
        samples = adapter.collect(["synthetic.xml"])
        cases = (
            ("2026-08-21T08:59:59.000000Z", 0, False),
            ("2026-08-21T09:00:30.000000Z", 0, False),
            ("2026-08-21T09:01:00.000000Z", 2, True),
        )
        for now, expected_samples, expected_complete in cases:
            with self.subTest(now=now):
                window = correlate_after_window(
                    observing_started_at="2026-08-21T09:00:00.000000Z", contract=_contract(),
                    samples=samples, now=now,
                )
                self.assertEqual(len(window.samples), expected_samples)
                self.assertEqual(window.complete, expected_complete)
