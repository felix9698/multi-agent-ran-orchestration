"""Hardware-free Gate 4 matrix for the three OBJ1 objective families."""

from __future__ import annotations

from datetime import timedelta
import unittest

from assurance.collector.mock_source import MockMeasurementSource
from assurance.collector.samples import ClockHealth, RawSample
from assurance.core.addressing import content_hash
from assurance.core.provenance import Provenance, TypedQuantity
from assurance.gateway.mock_adapter import FaultInjection
from assurance.objectives.family import ScenarioName
from assurance.objectives.qos_and_tsp import QoSandTSPFamily
from assurance.objectives.qos_target import QoSTargetFamily
from assurance.objectives.traffic_steering import TrafficSteeringPreferenceFamily
from assurance.contracts.capability import DeploymentBinding, TransportSecurity
from assurance.core.envelopes import ASSURANCE_SCHEMA_VERSION
from assurance.core.timebase import format_utc, parse_utc
from tests.assurance.objective_harness import FamilyCase, ObjectiveMatrixMixin


def _binding() -> DeploymentBinding:
    return DeploymentBinding(
        contract_id="deployment/obj1-mock",
        version="1.0.0",
        schema_version=ASSURANCE_SCHEMA_VERSION,
        document_status="NORMATIVE",
        standard_mapping={"a1p": "2", "e2sm-rc": "1.03"},
        endpoint_id="r1",
        base_url="https://r1.lab.invalid",
        transport_security=TransportSecurity.MTLS,
        secret_refs={"clientSecret": "env:R1_CLIENT_SECRET"},
        trust_anchor_ref="file:/etc/ssl/r1-ca.pem",
    )


def _collector(bundle, scenario: ScenarioName, start: str) -> MockMeasurementSource:
    from assurance.contracts.measurement import MeasurementSource

    instant = parse_utc(start)
    sample_scope = dict(bundle.sample_scope)
    samples_by_instant = {}
    for counter in bundle.counters:
        measurement = next(item for item in bundle.measurements if item.counter_id == counter.counter_id)
        if scenario is ScenarioName.MISSING:
            continue
        if scenario is ScenarioName.STALE:
            observed = instant - timedelta(milliseconds=measurement.cadence_ms * 2)
        else:
            observed = instant
        if counter.counter_id.endswith("serving-cell"):
            value = 87654321.0 if scenario is not ScenarioName.NEGATIVE else 12345678.0
            unit = "nci"
        elif counter.counter_id.endswith("drb-ue-thp-dl"):
            value = 700.0 if scenario is not ScenarioName.NEGATIVE else 100.0
            unit = "kbit/s"
        elif counter.counter_id.endswith("application-qoe-score"):
            value = 0.85 if scenario is not ScenarioName.NEGATIVE else 0.40
            unit = "qoe-score"
        elif counter.counter_id.endswith("snssai-core-throughput"):
            value = 700.0 if scenario is not ScenarioName.NEGATIVE else 100.0
            unit = "kbit/s"
        else:
            value = 35.0 if scenario is not ScenarioName.NEGATIVE else 95.0
            unit = "percent"
        sample_count = int(measurement.hold_ms // measurement.cadence_ms) + 1
        for index in range(sample_count):
            sample_id = f"{bundle.family}-{counter.counter_id}-{index}"
            sample = RawSample(
                sample_id=sample_id,
                counter_id=counter.counter_id,
                value=TypedQuantity(value, unit, Provenance.MEASURED, "obj1-mock"),
                scope_snapshot=sample_scope,
                observed_at=format_utc(observed + timedelta(milliseconds=measurement.cadence_ms * index)),
                cadence_ms=measurement.cadence_ms,
                clock_health=ClockHealth.SYNCHRONISED,
                trace_hash=content_hash({"sampleId": sample_id, "counterId": measurement.counter_id}),
                sequence=index,
            )
            samples_by_instant.setdefault(sample.observed_at, []).append(sample)
    script = [tuple(
        sample
        for at in sorted(samples_by_instant)
        for sample in samples_by_instant[at]
    )]
    return MockMeasurementSource(script=script, scope=sample_scope, source_id="obj1-mock")


def _case(family, case_id: str) -> FamilyCase:
    bundle = family.contract_bundle(
        scope={"ueId": "ue-1", "cellId": "NRCellDU-1"},
        deployment_binding=_binding(),
    )
    axes = bundle.configuration_axes()
    tick_ms = min(measurement.cadence_ms for measurement in bundle.measurements)
    observation_ticks = int(bundle.target.hold_ms // tick_ms) + 1
    def faults_for(scenario: ScenarioName):
        if scenario is ScenarioName.PARTIAL_EFFECT and len(axes) == 1:
            return FaultInjection(fail_axes=frozenset(axes))
        return None
    return FamilyCase(
        bundle=bundle,
        expectations=family.hardware_free_expectations(),
        collector_for=lambda scenario, start: _collector(bundle, scenario, start),
        case_id=case_id,
        evidence_cell_id=f"cell/{case_id}",
        faults_for=faults_for,
        tick_ms=tick_ms,
        settle_ms=tick_ms,
        observation_ticks=observation_ticks,
    )


class TrafficSteeringMatrixTests(ObjectiveMatrixMixin, unittest.TestCase):
    def make_case(self) -> FamilyCase:
        return _case(TrafficSteeringPreferenceFamily(), "obj1-tsp")


class QoSTargetMatrixTests(ObjectiveMatrixMixin, unittest.TestCase):
    def make_case(self) -> FamilyCase:
        return _case(QoSTargetFamily(), "obj1-qos")


class QoSandTSPMatrixTests(ObjectiveMatrixMixin, unittest.TestCase):
    def make_case(self) -> FamilyCase:
        return _case(QoSandTSPFamily(), "obj1-qos-tsp")


class QoSSteeringActuatorContractTests(unittest.TestCase):
    """QoS semantics stay in predicates while the released actuator steers."""

    def _bundle(self, family):
        return family.contract_bundle(
            scope={"ueId": "ue-1", "cellId": "NRCellDU-1"},
            deployment_binding=_binding(),
        )

    def test_both_qos_families_have_one_cell_steering_actuator_and_axis(self) -> None:
        for family in (QoSTargetFamily(), QoSandTSPFamily()):
            with self.subTest(family=family.family):
                bundle = self._bundle(family)
                self.assertEqual(bundle.configuration_axes(), ("servingCell",))
                self.assertEqual(
                    family.candidate_parameters(),
                    {"servingCell": ("12345678", "87654321")},
                )
                self.assertEqual(len(bundle.actuators), 1)
                actuator = bundle.actuators[0]
                self.assertEqual(actuator.policy_type_id, "AIC_UECellSteering_1.0.0")
                self.assertEqual(
                    dict(actuator.service_model),
                    {
                        "serviceModel": "E2SM-RC",
                        "style": "3",
                        "action": "1",
                        "profile": "E2SM-RC-STYLE3-ACTION1",
                    },
                )

    def test_qos_target_keeps_o1_and_kpm_format3_qos_predicates(self) -> None:
        bundle = self._bundle(QoSTargetFamily())
        self.assertEqual(
            {predicate.predicate_id for predicate in bundle.target.predicates},
            {"dl-prb-headroom", "ue-throughput-floor"},
        )
        counters = {
            counter.deployment_counter_name: counter
            for counter in bundle.counters
        }
        self.assertIn("RRU.PrbDl", counters)
        self.assertIn("DRB.UEThpDl", counters)
        self.assertIn("UE.ServingCell", counters)

    def test_composite_has_both_components_mandatory_in_one_target(self) -> None:
        bundle = self._bundle(QoSandTSPFamily())
        self.assertEqual(
            dict(bundle.component_predicates),
            {
                "QoSTarget": ("dl-prb-headroom", "ue-throughput-floor"),
                "TrafficSteeringPreference": (
                    "serving-cell-preferred-min",
                    "serving-cell-preferred-max",
                ),
            },
        )
        mandatory = {predicate.predicate_id for predicate in bundle.target.predicates}
        self.assertEqual(
            mandatory,
            {
                "dl-prb-headroom",
                "ue-throughput-floor",
                "serving-cell-preferred-min",
                "serving-cell-preferred-max",
            },
        )
        self.assertEqual(len(bundle.target.options), 1)

    def test_qos_kpi_declarations_match_the_judgement_contract_timing(self) -> None:
        for family in (QoSTargetFamily(), QoSandTSPFamily()):
            with self.subTest(family=family.family):
                bundle = self._bundle(family)
                measurements = {
                    measurement.contract_id: measurement
                    for measurement in bundle.measurements
                }
                for declaration in family.kpi_declaration():
                    measurement = measurements[declaration.measurement_ref]
                    self.assertEqual(
                        declaration.cadence_ms,
                        measurement.cadence_ms,
                    )
                    self.assertEqual(
                        declaration.freshness_bound_ms,
                        measurement.freshness_bound_ms,
                    )
