"""Gate 5 stage 1: the live contract instances, driven hardware-free.

Gate 4 proved each family's contracts over contracts of its own making.  This
file proves the *live* instances -- the ones built against the committed live
binding, the real cells 12345678 and 87654321, and the UE identity shape the
deployment actually publishes -- go through the same Kernel to the same
terminals over :class:`~assurance.gateway.mock_adapter.MockActuationAdapter`.

That is the whole point of stage 1: everything that can be wrong about a Gate 5
run except the radio is wrong here first, and the radio is switched off.

The historical families retain their single-counter collector.  The QoS
families now build the same runtime with configuration-readback, O1 PM, and
E2 KPM collectors sharing one grid. ``SliceSLATarget`` remains the
no-deployed-policy-type refusal; its separate project type is a hardware-free
artifact.
"""

from __future__ import annotations

import unittest
from typing import Any, Dict, Mapping, Sequence

from assurance.collector.samples import ClockHealth, RawSample
from assurance.contracts.live_binding import load_assurance_live_binding
from assurance.contracts.validation import validate_family_set
from assurance.core.addressing import content_hash
from assurance.core.axes import EvidenceCellStatus, TrialOutcome
from assurance.core.provenance import Provenance, TypedQuantity
from assurance.core.timebase import format_utc, parse_utc
from assurance.core.states import TrialState
from assurance.gateway.mock_adapter import MockActuationAdapter
from assurance.live.objective_runtime import (
    build_live_objective_runtime,
    bundle_geometry,
)
from assurance.live.pin_to_cell_driver import KpmUeAttributionReader, LiveUeObservation
from assurance.objectives import FAMILY_MODULES, RegistryError, record_for

BINDING = "deployment/assurance-live-binding.1.0.0.json"
HOME_NCI, TARGET_NCI = 12345678, 87654321
AMF_UE_NGAP_ID = 131

ACTUABLE = (
    "TrafficSteeringPreference", "UELevelTarget", "QoSTarget", "QoSandTSP",
)
SINGLE_COUNTER = ("TrafficSteeringPreference", "UELevelTarget")
NOT_ACTUABLE = ("SliceSLATarget",)

LIVE_SCOPE: Mapping[str, Any] = {
    "ueId": str(AMF_UE_NGAP_ID),
    "cellId": "NRCellDU-1",
    "targetServingCell": str(TARGET_NCI),
    "homeServingCell": str(HOME_NCI),
}


class _Clock:
    """A virtual clock; nothing here waits on anything."""

    def __init__(self) -> None:
        self.ms = 0

    def now(self) -> str:
        from datetime import datetime, timedelta, timezone

        moment = datetime(2026, 8, 25, tzinfo=timezone.utc) + timedelta(milliseconds=self.ms)
        return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def monotonic_ms(self) -> int:
        return self.ms

    def sleep_ms(self, ms: int) -> None:
        self.ms += max(0, int(ms))


def _identity() -> LiveUeObservation:
    return LiveUeObservation(
        amf_ue_ngap_id=AMF_UE_NGAP_ID,
        gu_ami={
            "plmnId": {"mcc": "208", "mnc": "95"},
            "amfRegionId": "01", "amfSetId": "040", "amfPointer": "04",
        },
        serving_nci=HOME_NCI,
        e2_node="ngran=02;plmn=208-095-2;nb=0000003584/00;cudu=none:00000000000000000000",
        connection_epoch=173,
        observed_at="2026-08-25T00:00:00.000000Z",
        trace_hash="0" * 64,
    )


class LiveObjectiveFixture(unittest.TestCase):

    def build(
        self,
        family: str,
        *,
        observed: Sequence[int] = (),
        counter_sample_loaders: Mapping[str, Any] | None = None,
    ) -> Any:
        self.binding = load_assurance_live_binding(BINDING)
        self.clock = _Clock()
        self.adapter = MockActuationAdapter(config={"servingCell": str(HOME_NCI)})
        self.adapter.hosts_watchdogs = False
        reader = KpmUeAttributionReader(
            read_new_lines=lambda: (),
            topology=__import__(
                "assurance.live.pin_to_cell_driver", fromlist=["LiveCellTopology"]
            ).LiveCellTopology(
                plmn={"mcc": "208", "mnc": "95"},
                nb_id_to_nci={0xE00: HOME_NCI, 0xB00: TARGET_NCI},
                expected_epochs=dict(self.binding.kpm_expected_epochs),
            ),
        )
        return build_live_objective_runtime(
            family_module=FAMILY_MODULES[family](),
            scope=LIVE_SCOPE,
            binding=self.binding,
            policy_port=None,
            policy_builder_factory=lambda kernel, bundle: (lambda command: {}),
            reader=reader,
            identity=_identity(),
            now=self.clock.now,
            monotonic_ms=self.clock.monotonic_ms,
            sleep_ms=self.clock.sleep_ms,
            case_id=f"case/g5-dryrun:{family}",
            adapter_name="mock",
            adapter_override=self.adapter,
            counter_sample_loaders=(
                counter_sample_loaders
                if counter_sample_loaders is not None
                else {
                    "counter/rru-prb-dl": lambda: (),
                    "counter/kpm-f3-drb-ue-thp-dl": lambda: (),
                }
            ),
        )


class TheLiveBundlesAreCrossConsistent(LiveObjectiveFixture):

    def test_each_actuable_family_builds_a_valid_live_family_set(self) -> None:
        for family in ACTUABLE:
            with self.subTest(family=family):
                runtime = self.build(family)
                b = runtime.bundle
                contracts = [
                    *b.counters, *b.measurements, b.target, b.vector, b.release,
                    b.case_policy, *b.watchdogs, b.harm, b.deployment,
                    *b.actuators, *b.capabilities, b.composition,
                ]
                validate_family_set(contracts)

    def test_the_live_cells_are_the_deployment_s_own(self) -> None:
        for family in ACTUABLE:
            with self.subTest(family=family):
                runtime = self.build(family)
                self.assertEqual(
                    dict(runtime.bundle.baseline_config), {"servingCell": str(HOME_NCI)}
                )
                params = dict(runtime.bundle.target.options[0].parameter_space)
                self.assertEqual(params["servingCell"], (str(TARGET_NCI),))
                self.assertIn(HOME_NCI, self.binding.cells)
                self.assertIn(TARGET_NCI, self.binding.cells)

    def test_every_actuable_family_names_the_one_policy_type_this_deployment_has(
        self,
    ) -> None:
        for family in ACTUABLE:
            with self.subTest(family=family):
                lifecycle = FAMILY_MODULES[family]().policy_lifecycle()
                self.assertEqual(
                    lifecycle.policy_type_id, self.binding_policy_type()
                )

    def binding_policy_type(self) -> str:
        return load_assurance_live_binding(BINDING).r1.policy_type_id

    def test_each_declared_counter_has_its_own_geometry_and_collector(self) -> None:
        """A missing or wrongly stamped counter would leave a predicate unread."""
        for family in ACTUABLE:
            with self.subTest(family=family):
                runtime = self.build(family)
                declared = {m.counter_id for m in runtime.bundle.measurements}
                geometry_ids = {item.counter_id for item in runtime.geometry.counters}
                self.assertEqual(geometry_ids, declared)
                source = runtime.collector.describe_source()
                collector_ids = (
                    {source["counterId"]} if len(declared) == 1
                    else {item["counterId"] for item in source["sources"]}
                )
                self.assertEqual(collector_ids, declared)
                self.assertEqual(
                    runtime.geometry.cadence_ms,
                    runtime.bundle.measurements[0].cadence_ms,
                )

    def test_qos_target_builds_the_three_counter_live_runtime(self) -> None:
        runtime = self.build("QoSTarget")
        self.assertEqual(
            {item.deployment_counter_name for item in runtime.geometry.counters},
            {"UE.ServingCell", "RRU.PrbDl", "DRB.UEThpDl"},
        )
        self.assertEqual(runtime.collector.describe_source()["kind"], "multi-counter")
        self.assertEqual(len(runtime.collector.collectors), 3)

    def test_qos_mixed_cadences_share_an_anchor_and_correlate(self) -> None:
        sources: Dict[str, Any] = {}
        runtime = self.build("QoSandTSP", counter_sample_loaders={
            "counter/rru-prb-dl": lambda: sources["prb"](),
            "counter/kpm-f3-drb-ue-thp-dl": lambda: sources["throughput"](),
        })
        by_counter = {
            item.deployment_counter_name: item
            for item in runtime.geometry.counters
        }
        self.assertEqual(by_counter["UE.ServingCell"].cadence_ms, 1000)
        self.assertEqual(by_counter["RRU.PrbDl"].cadence_ms, 60000)
        self.assertEqual(by_counter["DRB.UEThpDl"].cadence_ms, 60000)
        self.assertEqual(runtime.geometry.cadence_ms, 1000)
        self.assertTrue({
            "serving-cell-preferred-min", "serving-cell-preferred-max",
        }.issubset({p.predicate_id for p in runtime.bundle.target.predicates}))

        sources.update({
            "prb": _RawSeries(runtime, "RRU.PrbDl", 40.0, "percent",
                              {"nrCellDu": "1"}),
            "throughput": _RawSeries(
                runtime, "DRB.UEThpDl", 600.0, "kbit/s",
                {"amf_ue_ngap_id": str(AMF_UE_NGAP_ID)},
            ),
        })
        runtime.collector.collectors[0]._reader = _Series(  # noqa: SLF001
            runtime, [TARGET_NCI] * 121
        )
        report = runtime.path.run_trial(
            runtime.candidate_id(), cell_id=runtime.cell_id,
            observation_ticks=121, tick_ms=1000, settle_ms=1000,
        )
        self.assertEqual(report.outcome, TrialOutcome.SUCCESS)
        emitted = runtime.collector.emitted()
        cadences = {
            geometry.counter_id: {
                sample.cadence_ms for sample in emitted
                if sample.counter_id == geometry.counter_id
            }
            for geometry in by_counter.values()
        }
        self.assertEqual(cadences[by_counter["UE.ServingCell"].counter_id], {1000})
        self.assertEqual(cadences[by_counter["RRU.PrbDl"].counter_id], {60000})
        self.assertEqual(cadences[by_counter["DRB.UEThpDl"].counter_id], {60000})

    def test_qos_target_reads_o1_and_kpm_and_evaluates_both_predicates(self) -> None:
        sources: Dict[str, Any] = {}
        loaders = {
            "counter/rru-prb-dl": lambda: sources["prb"](),
            "counter/kpm-f3-drb-ue-thp-dl": lambda: sources["throughput"](),
        }
        runtime = self.build("QoSTarget", counter_sample_loaders=loaders)
        sources.update({
            "prb": _RawSeries(
                runtime, "RRU.PrbDl", 40.0, "percent",
                {"nrCellDu": "1"},
            ),
            "throughput": _RawSeries(
                runtime, "DRB.UEThpDl", 600.0, "kbit/s",
                {"amf_ue_ngap_id": str(AMF_UE_NGAP_ID)},
            ),
        })
        runtime.collector.collectors[0]._reader = _Series(  # noqa: SLF001
            runtime, [TARGET_NCI] * 121
        )
        report = runtime.path.run_trial(
            runtime.candidate_id(),
            cell_id=runtime.cell_id,
            observation_ticks=121,
            tick_ms=runtime.geometry.cadence_ms,
            settle_ms=runtime.geometry.cadence_ms,
        )
        evaluation = runtime.kernel.reduced_state()["trials"][report.trial_id]["evaluation"]
        self.assertEqual(report.outcome, TrialOutcome.SUCCESS, evaluation)
        self.assertEqual(
            evaluation["predicateVerdicts"],
            {"dl-prb-headroom": "PASS", "ue-throughput-floor": "PASS"},
        )

    def test_qos_target_fails_closed_when_one_counter_trace_is_missing(self) -> None:
        sources: Dict[str, Any] = {}
        runtime = self.build("QoSTarget", counter_sample_loaders={
            "counter/rru-prb-dl": lambda: sources["prb"](),
            "counter/kpm-f3-drb-ue-thp-dl": lambda: (),
        })
        sources["prb"] = _RawSeries(
            runtime, "RRU.PrbDl", 40.0, "percent", {"nrCellDu": "1"}
        )
        runtime.collector.collectors[0]._reader = _Series(  # noqa: SLF001
            runtime, [TARGET_NCI] * 121
        )
        report = runtime.path.run_trial(
            runtime.candidate_id(),
            cell_id=runtime.cell_id,
            observation_ticks=121,
            tick_ms=runtime.geometry.cadence_ms,
            settle_ms=runtime.geometry.cadence_ms,
        )
        evaluation = runtime.kernel.reduced_state()["trials"][report.trial_id]["evaluation"]
        self.assertEqual(report.outcome, TrialOutcome.INDETERMINATE)
        self.assertEqual(evaluation["measurementSufficiency"], "MISSING_INTERVAL")
        self.assertEqual(evaluation["predicateVerdicts"]["ue-throughput-floor"],
                         "INDETERMINATE")


class TheLiveBundlesRunToTerminals(LiveObjectiveFixture):
    """Admission -> finalize, and admission -> rollback, on the live contracts."""

    def drive(self, family: str, observed: Sequence[int]) -> Any:
        runtime = self.build(family)
        geom = runtime.geometry
        anchor_source = _Series(runtime, observed)
        runtime.collector._reader = anchor_source  # noqa: SLF001 - scripted source
        report = runtime.path.run_trial(
            runtime.candidate_id(),
            cell_id=runtime.cell_id,
            observation_ticks=int(geom.hold_ms // geom.cadence_ms) + 1,
            tick_ms=geom.cadence_ms,
            settle_ms=geom.cadence_ms,
        )
        self.runtime = runtime
        return report

    def test_a_held_target_finalizes_live(self) -> None:
        for family in SINGLE_COUNTER:
            with self.subTest(family=family):
                report = self.drive(family, [TARGET_NCI] * 12)
                self.assertEqual(report.terminal_state, TrialState.SETTLED_SUCCESS)
                self.assertEqual(report.outcome, TrialOutcome.SUCCESS)
                self.assertEqual(
                    report.evidence_status, EvidenceCellStatus.CLOSED_PASS.value
                )
                self.assertEqual(
                    self.adapter.snapshot(), {"servingCell": str(TARGET_NCI)}
                )

    def test_a_target_that_does_not_hold_rolls_back_to_the_baseline(self) -> None:
        for family in SINGLE_COUNTER:
            with self.subTest(family=family):
                report = self.drive(
                    family, [TARGET_NCI, TARGET_NCI, HOME_NCI, TARGET_NCI] * 3
                )
                self.assertEqual(report.terminal_state, TrialState.SETTLED_NON_SUCCESS)
                self.assertEqual(report.outcome, TrialOutcome.FAIL)
                self.assertEqual(
                    self.adapter.snapshot(), {"servingCell": str(HOME_NCI)}
                )


class TheRefusalsAreTheFinding(unittest.TestCase):

    def test_a_family_with_no_deployed_policy_type_is_refused_not_wired(self) -> None:
        binding = load_assurance_live_binding(BINDING)
        for family in NOT_ACTUABLE:
            with self.subTest(family=family):
                module = FAMILY_MODULES[family]()
                record = record_for(family)
                self.assertIsNone(record.standard_mapping.policy_type_id)
                self.assertFalse(record.deployment_capability.submittable)
                self.assertFalse(record.deployment_capability.a1_policy_type_present)
                self.assertNotEqual(
                    module.policy_lifecycle().policy_type_id,
                    binding.r1.policy_type_id,
                )
                with self.assertRaisesRegex(
                    RegistryError, "A1_SLICE_TYPE_NOT_DEPLOYED"
                ):
                    build_live_objective_runtime(
                        family_module=module, scope=LIVE_SCOPE, binding=binding,
                        policy_port=None, policy_builder_factory=lambda k, b: None,
                        reader=None, identity=_identity(),
                        now=lambda: "2026-08-25T00:00:00.000000Z",
                        monotonic_ms=lambda: 0, sleep_ms=lambda _ms: None,
                        case_id="case/refused",
                    )

    def test_the_deployment_advertises_one_objective_and_one_control_axis(self) -> None:
        """The admission gate the refusal rests on, read from the manifest.

        The manifest lives outside the repository, so its location is taken
        from the runner's own constant rather than written out here -- a host
        path spelled into a test is what ``tests/test_portability.py`` refuses,
        and it would also go stale silently if the deployment moved.  Where the
        deployment is not present the assertion has nothing to make, so it is
        skipped rather than passed.
        """
        import json
        from pathlib import Path

        from tools.g3ota.run_ota import DEFAULT_CAPABILITY

        manifest_path = Path(DEFAULT_CAPABILITY)
        if not manifest_path.is_file():
            self.skipTest("the live deployment's capability manifest is not present")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["objectives"], ["PIN_TO_CELL"])
        self.assertEqual(manifest["controlAxes"], ["serving_cell"])


class _Series:
    """A scripted stand-in for the KPM reader, one observation per grid slot."""

    def __init__(self, runtime: Any, observed: Sequence[int]) -> None:
        self._runtime = runtime
        self._observed = list(observed)
        self._calls = 0

    def refresh(self, **_kwargs: Any) -> tuple:
        return ()

    def at_or_before(self, instant: str, **_kwargs: Any) -> Any:
        if not self._observed:
            return None
        value = self._observed[min(self._calls, len(self._observed) - 1)]
        self._calls += 1
        return LiveUeObservation(
            amf_ue_ngap_id=AMF_UE_NGAP_ID,
            gu_ami={"plmnId": {"mcc": "208", "mnc": "95"}, "amfRegionId": "01",
                    "amfSetId": "040", "amfPointer": "04"},
            serving_nci=int(value), e2_node="node", connection_epoch=173,
            observed_at=instant, trace_hash="1" * 64,
        )


class _RawSeries:
    """One real-shaped wire sample at each deterministic collector poll."""

    def __init__(
        self, runtime: Any, counter: str, value: float, unit: str,
        scope: Mapping[str, str],
    ) -> None:
        self._runtime = runtime
        self._counter = counter
        self._value = value
        self._unit = unit
        self._scope = scope
        self._sequence = 0

    def __call__(self) -> tuple[RawSample, ...]:
        from datetime import timedelta

        now = self._runtime.clock()
        instants = [now]
        if self._sequence == 0:
            instants.insert(0, format_utc(
                parse_utc(now) - timedelta(
                    milliseconds=self._runtime.geometry.cadence_ms
                )
            ))
        samples = []
        for observed_at in instants:
            sequence = self._sequence
            self._sequence += 1
            trace_hash = content_hash({
                "wireCounter": self._counter, "observedAt": observed_at,
                "sequence": sequence,
            })
            sample_id = content_hash(
                {"traceHash": trace_hash, "counter": self._counter}
            )
            samples.append(RawSample(
                sample_id=sample_id,
                counter_id=self._counter,
                value=TypedQuantity(
                    self._value, self._unit, Provenance.MEASURED, sample_id
                ),
                scope_snapshot=self._scope,
                observed_at=observed_at,
                cadence_ms=1000,
                clock_health=ClockHealth.SYNCHRONISED,
                trace_hash=trace_hash,
                sequence=sequence,
            ))
        return tuple(samples)


if __name__ == "__main__":
    unittest.main()
