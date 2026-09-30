"""The PIN_TO_CELL regression case, expressed in the new contract families.

Not a test module.  Gate 3 runs the known physical steering case through the
new architecture (task section 13, Gate 3), which first requires saying what
that case *is* in the vocabulary of ``assurance/contracts`` rather than in the
vocabulary of the frozen ``AIC_UECellSteering_1.0.0`` policy object.  This
module is that statement, and ``test_pin_to_cell_contract.py`` is the proof
that the Kernel judges it the way the frozen contract does.

The mapping is mostly mechanical -- ``steeringObjective.kind`` becomes an
objective family, ``actionEnvelope.allowedCells`` becomes a one-point
parameter space, ``constraints.requiredKpiFreshnessMs`` becomes a measurement
contract's freshness bound.  One part is not, and it is the reason this module
exists.

**The mandatory predicate is an identity, not a rate.**  PIN_TO_CELL succeeds
when the UE is on the pinned cell, and the Kernel's evaluator aggregates
numbers: it reads ``float(sample.value)`` and collapses a window with MEAN,
P95, MIN and so on.  A cell identity is not a quantity.  The frozen schema
already carries the cell as an integer ``CId.ncI``, so the identity travels as
that integer, and the predicate becomes a *pair*:

    MIN(nCI) == pinned  AND  MAX(nCI) == pinned

over the same counter.  Two measurement contracts, two mandatory predicates,
and together they say something stronger than the readback the old path
checked once: **every sample in every completed window of the hold observed
exactly the pinned cell**.  A UE that moves away and back inside the hold
fails MIN or MAX, where a single end-of-episode readback would have reported
success.  No frozen type changes to say it.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from assurance.advisors.coordinator import StrategyBackedEvidenceCoordinator
from assurance.advisors.strategies import DeterministicStrategy
from assurance.collector.mock_source import MockMeasurementSource
from assurance.collector.samples import ClockHealth, RawSample
from assurance.contracts import (
    ActuatorBinding,
    ActuatorPath,
    Aggregation,
    CapabilityManifest,
    CertifiedHarmBound,
    ClockRequirement,
    ComparisonOperator,
    CompositionManifest,
    CoordinationCasePolicy,
    CounterBinding,
    DeploymentBinding,
    EvidenceCell,
    Estimator,
    GapPolicy,
    HarmContract,
    HarmKind,
    MeasurementContract,
    MeasurementSource,
    OverlapPolicy,
    TargetContract,
    TargetOption,
    TargetPredicate,
    TargetReleasePolicy,
    TargetVector,
    TransportSecurity,
    TypedConstraint,
    UncertaintyRule,
    WatchdogAction,
    WatchdogContract,
)
from assurance.core.addressing import content_hash
from assurance.core.axes import EvidenceCellStatus
from assurance.core.envelopes import ASSURANCE_SCHEMA_VERSION
from assurance.core.provenance import Provenance, TypedQuantity
from assurance.core.timebase import format_utc, parse_utc
from assurance.gateway.gateway import TokenBoundWriteGateway
from assurance.gateway.journal import InMemoryTransactionJournal
from assurance.gateway.mock_adapter import FaultInjection, MockActuationAdapter
from assurance.kernel.event_store import MemoryEventStore
from assurance.kernel.kernel import AssuranceKernel
from assurance.kernel.reducer import KernelReducer
from assurance.vertical import SteppingClock, VerticalDeployment, VerticalPath

from tests.assurance.vertical_support import confirmation_for

START = "2026-08-21T09:00:00.000000Z"

#: The frozen policy type this case actuates through.
POLICY_TYPE_ID = "AIC_UECellSteering_1.0.0"

#: ``CId.ncI`` values, taken from the frozen golden vectors so this case names
#: the cells the shared contract bundle already names.  Integers in the frozen
#: schema, which is what lets a cell identity travel as a measured quantity at
#: all.  The PLMN they sit under is a deployment fact and lives in the
#: capability manifest, not in the candidate: what a trial varies is which
#: cell, not which network.
HOME_NCI = 12345678
TARGET_NCI = 87654321

#: The configuration surface: one axis, because PIN_TO_CELL's frozen contract
#: fixes ``maxActuationsPerEpisode`` at 1.
BASELINE_CONFIG: Mapping[str, Any] = {"servingCell": str(HOME_NCI)}

#: For a steering objective the safe thing to do with a UE is leave it where
#: it was, so the contracted safe state *is* the baseline.  Judgement 1 of
#: SEAMS-GATE2.md section 8.3 then settles an Emergency Stop as
#: ``OPERATOR_ABORT`` rather than locking it down.
SAFE_STATE: Mapping[str, Any] = dict(BASELINE_CONFIG)

SCOPE: Mapping[str, Any] = {"ueId": "ue-1"}
ADAPTER = "r1"
CASE_ID = "case/pin-to-cell"
CELL_ID = "cell/pin-to-cell"
ACTIVE_VECTOR = "target/pin-to-cell"
COUNTER_ID = "counter/serving-cell"
SAMPLE_SCOPE = {"ueId": "ue-1"}

CADENCE_MS = 1000
WINDOW_MS = 3000
HOLD_MS = 3000
FRESHNESS_MS = 2000

#: ``CId.ncI`` is a count of nothing; the unit token says what the number is
#: an identity of rather than pretending it is dimensionless.
NCI_UNIT = "nci"

IDENTITY = {
    "version": "1.0.0",
    "schema_version": ASSURANCE_SCHEMA_VERSION,
    "document_status": "NORMATIVE",
    "standard_mapping": {"a1p": "1.0", "e2sm-rc": "1.0", "o1": "1.0"},
}


def identity(contract_id: str) -> Dict[str, Any]:
    return {**IDENTITY, "contract_id": contract_id}


def quantity(value: float, unit: str, source: str) -> TypedQuantity:
    return TypedQuantity(value, unit, Provenance.EXPERIMENT_CONFIG, source)


def serving_cell_measurement(
    contract_id: str, *, aggregation: Aggregation
) -> MeasurementContract:
    """One end of the identity predicate pair.

    Both ends read the same counter and differ only in how they collapse the
    window.  The uncertainty parameter is exactly zero: an identity has no
    measurement error, and a non-zero margin would let ``EQUAL`` accept a
    neighbouring ``nCI``.
    """
    return MeasurementContract(
        **identity(contract_id),
        counter_id=COUNTER_ID,
        scope_selector={"ueId": "ue-1"},
        membership_snapshot=("ue-1",),
        cadence_ms=CADENCE_MS,
        window_width_ms=WINDOW_MS,
        window_stride_ms=WINDOW_MS,
        overlap=OverlapPolicy.DISJOINT,
        aggregation=aggregation,
        # MIN and MAX are the 0th and 100th empirical order statistics; the
        # estimator names which convention produced them, and for an exact
        # extremum there is only one.
        estimator=Estimator.EMPIRICAL_QUANTILE,
        minimum_entity_count=1,
        hold_ms=HOLD_MS,
        gap_policy=GapPolicy.CONSERVATIVE_CHARGE,
        missing_interval_charge=quantity(5.0, "ms", "plan/pin-missing-charge"),
        freshness_bound_ms=FRESHNESS_MS,
        clock_requirement=ClockRequirement.SYNCHRONISED_REQUIRED,
        uncertainty_rule=UncertaintyRule(
            "exact_identity", quantity(0.0, NCI_UNIT, "calibration/serving-cell")
        ),
    )


def contract_set(*, pinned_nci: int = TARGET_NCI) -> Dict[str, Any]:
    """The PIN_TO_CELL case as a complete, cross-consistent contract family."""
    counter = CounterBinding(
        counter_id=COUNTER_ID,
        deployment_counter_name="observedServingCell",
        # The contracted effect readback itself, not a performance count --
        # the frozen status object's ``readback.observedServingCell``.
        source=MeasurementSource.CONFIGURATION_READBACK,
        scope_keys=("ueId",),
        unit=NCI_UNIT,
        native_cadence_ms=CADENCE_MS,
        deployment_binding_ref="deployment/r1",
    )
    floor = serving_cell_measurement(
        "measurement/serving-cell-min", aggregation=Aggregation.MIN
    )
    ceiling = serving_cell_measurement(
        "measurement/serving-cell-max", aggregation=Aggregation.MAX
    )
    pinned = quantity(float(pinned_nci), NCI_UNIT, "plan/pinned-cell")
    predicates = (
        TargetPredicate(
            "serving-cell-is-pinned-throughout",
            TypedConstraint(
                "measurement/serving-cell-min", ComparisonOperator.EQUAL, pinned
            ),
            description="no sample in the hold observed a lower cell identity",
        ),
        TargetPredicate(
            "serving-cell-is-pinned-only",
            TypedConstraint(
                "measurement/serving-cell-max", ComparisonOperator.EQUAL, pinned
            ),
            description="no sample in the hold observed a higher cell identity",
        ),
    )
    option = TargetOption(
        **identity("option/pin-to-cell"),
        capability_ref="capability/ue-cell-steering",
        # Exactly one point: the frozen policy schema refuses a PIN_TO_CELL
        # whose ``allowedCells`` has more than one member, so the candidate
        # universe for this target has cardinality 1.
        parameter_space={"servingCell": (str(pinned_nci),)},
    )
    target = TargetContract(
        **identity("target/pin-to-cell"),
        objective_family="UeCellSteeringPinToCell",
        scope_selector={"ueId": "ue-1"},
        predicates=predicates,
        options=(option,),
        hold_ms=HOLD_MS,
    )
    vector = TargetVector(
        **identity("vector/pin-to-cell"), ordered_target_refs=("target/pin-to-cell",)
    )
    release = TargetReleasePolicy(**identity("release/pin-to-cell"))
    case_policy = CoordinationCasePolicy(
        **identity("case-policy/pin-to-cell"),
        deadline_ms=600_000,
        max_trials=2,
        max_proposals=4,
        target_release_policy_ref="release/pin-to-cell",
        harm_contract_refs=("harm/pin-to-cell",),
    )
    watchdog = WatchdogContract(
        **identity("watchdog/serving-cell"),
        watchdog_id="wd/serving-cell",
        trigger=TypedConstraint(
            "measurement/serving-cell-min", ComparisonOperator.EQUAL, pinned
        ),
        action=WatchdogAction.STOP_AND_ROLLBACK,
    )
    bound = CertifiedHarmBound(
        "bound/pin-to-cell",
        quantity(20.0, "ms", "calibration/pin-bound"),
        quantity(2.0, "ms", "calibration/pin-uncertainty"),
        quantity(22.0, "ms", "calibration/pin-conservative"),
        "measurement/serving-cell-min#uncertainty",
        {"ueId": "ue-1"},
        10_000,
        ("calibration/pin-1",),
        "proof/pin-to-cell-v1",
    )
    harm = HarmContract(
        **identity("harm/pin-to-cell"),
        harm_kind=HarmKind.TRIAL_INDUCED,
        scope_selector={"ueId": "ue-1"},
        reserve=quantity(100.0, "ms", "plan/pin-reserve"),
        bounds=(bound,),
        watchdogs=(watchdog,),
        missing_interval_charge=quantity(5.0, "ms", "plan/pin-missing-charge"),
    )
    deployment = DeploymentBinding(
        **identity("deployment/r1"),
        endpoint_id="r1",
        base_url="https://r1.lab.invalid",
        transport_security=TransportSecurity.MTLS,
        secret_refs={"clientSecret": "env:R1_CLIENT_SECRET"},
        trust_anchor_ref="file:/etc/ssl/r1-ca.pem",
    )
    actuator = ActuatorBinding(
        **identity("actuator/ue-cell-steering"),
        capability_ref="capability/ue-cell-steering",
        path=ActuatorPath.OFFICIAL_ORAN_DYNAMIC,
        policy_type_id=POLICY_TYPE_ID,
        service_model={"serviceModel": "E2SM-RC", "style": "3"},
        readback_measurement_ref="measurement/serving-cell-min",
        deployment_binding_ref="deployment/r1",
    )
    capability = CapabilityManifest(
        **identity("capability/ue-cell-steering"),
        capability_id="capability/ue-cell-steering",
        supported_objectives=("UeCellSteeringPinToCell",),
        constraints=(predicates[0].constraint,),
        actuator_refs=("actuator/ue-cell-steering",),
        measurement_refs=(
            "measurement/serving-cell-min",
            "measurement/serving-cell-max",
        ),
        interface_versions={"a1p": "1.0", "e2smRc": "1.0"},
    )
    composition = CompositionManifest(
        **identity("composition/pin-to-cell"),
        composition_id="composition/pin-to-cell",
        capability_refs=("capability/ue-cell-steering",),
    )
    return {
        "counter": counter,
        "measurement_min": floor,
        "measurement_max": ceiling,
        "target": target,
        "vector": vector,
        "release": release,
        "case_policy": case_policy,
        "watchdog": watchdog,
        "harm": harm,
        "deployment": deployment,
        "actuator": actuator,
        "capability": capability,
        "composition": composition,
    }


def serving_cell_series(
    *, start: str, observed: Sequence[int], source_id: str = "mock-serving-cell"
) -> MockMeasurementSource:
    """A scripted readback series, one ``nCI`` per cadence tick.

    Takes the observations one at a time rather than a single constant,
    because the whole point of the MIN/MAX pair is what it says about a series
    that is *not* constant.
    """
    from datetime import timedelta

    instant = parse_utc(start)
    step = timedelta(milliseconds=CADENCE_MS)
    script = []
    for index, nci in enumerate(observed):
        sample_id = f"{source_id}-{index}"
        script.append(
            (
                RawSample(
                    sample_id=sample_id,
                    counter_id=COUNTER_ID,
                    value=TypedQuantity(
                        float(nci), NCI_UNIT, Provenance.MEASURED, source_id
                    ),
                    scope_snapshot=dict(SAMPLE_SCOPE),
                    observed_at=format_utc(instant + step * index),
                    cadence_ms=CADENCE_MS,
                    clock_health=ClockHealth.SYNCHRONISED,
                    trace_hash=content_hash(
                        {"sampleId": sample_id, "counterId": COUNTER_ID}
                    ),
                    sequence=index,
                ),
            )
        )
    return MockMeasurementSource(
        script=script, scope=SAMPLE_SCOPE, source_id=source_id
    )


class PinToCellFixture:
    """The PIN_TO_CELL case wired end to end, hardware-free.

    Deliberately its own fixture rather than a parameter of the Gate 2
    steering one: they are different objectives with different measurement
    shapes, and a fixture that served both would have to be told which it was
    on every line.
    """

    def build(
        self,
        *,
        observed: Sequence[int] = (TARGET_NCI,) * 5,
        pinned_nci: int = TARGET_NCI,
        faults: Optional[FaultInjection] = None,
        config: Optional[Mapping[str, Any]] = None,
        adapter_hosts_watchdogs: bool = False,
    ) -> VerticalPath:
        self.clock = SteppingClock(START)
        self.contracts = contract_set(pinned_nci=pinned_nci)
        self.adapter = MockActuationAdapter(
            config=dict(config if config is not None else BASELINE_CONFIG),
            faults=faults,
        )
        # The A1 policy path cannot host a contract watchdog, so this fixture
        # defaults to the Kernel-hosted arming judgement 2 settled.
        self.adapter.hosts_watchdogs = adapter_hosts_watchdogs
        self.journal = InMemoryTransactionJournal()
        self.gateway = TokenBoundWriteGateway(
            adapters={ADAPTER: self.adapter},
            safe_state=SAFE_STATE,
            journal=self.journal,
            clock=self.clock,
        )
        self.store = MemoryEventStore()
        self.kernel = AssuranceKernel(
            event_store=self.store,
            reducer=KernelReducer(),
            write_gateway=self.gateway,
            measurement_collector=None,
        )
        self.epoch = self.admit_and_freeze()
        self.open_case()
        self.collector = serving_cell_series(start=START, observed=observed)
        self.path = VerticalPath(
            kernel=self.kernel,
            gateway=self.gateway,
            deployment=VerticalDeployment(
                adapter_name=ADAPTER,
                scope=SCOPE,
                baseline_config=dict(config if config is not None else BASELINE_CONFIG),
            ),
            case_id=CASE_ID,
            clock=self.clock,
            coordinator=StrategyBackedEvidenceCoordinator(
                strategy=DeterministicStrategy()
            ),
            collector=self.collector,
        )
        return self.path

    def admit_and_freeze(self) -> Any:
        data = self.contracts
        now = self.clock()
        vector_confirmation = confirmation_for(
            data["vector"], event_id="confirm/pin-vector", at=now
        )
        policy_confirmation = confirmation_for(
            data["case_policy"], event_id="confirm/pin-policy", at=now
        )
        for key in (
            "counter",
            "measurement_min",
            "measurement_max",
            "target",
            "release",
            "watchdog",
            "harm",
            "actuator",
            "capability",
            "composition",
        ):
            self.kernel.admit_contract(data[key], confirmation=None, now=now)
        self.kernel.admit_contract(
            data["vector"], confirmation=vector_confirmation, now=now
        )
        self.kernel.admit_contract(
            data["case_policy"], confirmation=policy_confirmation, now=now
        )
        self.kernel.admit_deployment(data["deployment"], now=now)
        return self.kernel.freeze_epoch(confirmation=vector_confirmation, now=now)

    def open_case(self) -> None:
        catalog = self.kernel.current_catalog()
        self.kernel.open_case(
            case_id=CASE_ID,
            policy=self.contracts["case_policy"],
            active_vector=ACTIVE_VECTOR,
            usable_reserve={
                "harm/pin-to-cell": quantity(
                    100.0, "ms", "plan/pin-reserve"
                ).to_canonical_dict()
            },
            reserve_per_trial={
                "harm/pin-to-cell": quantity(
                    20.0, "ms", "plan/pin-per-trial"
                ).to_canonical_dict()
            },
            evidence_cells=(
                EvidenceCell(
                    cell_id=CELL_ID,
                    target_ref="target/pin-to-cell",
                    candidate_semantic_hash=catalog.candidates[0].semantic_hash,
                    status=EvidenceCellStatus.OPEN,
                    required_independent_contributions=1,
                ),
            ),
            now=self.clock(),
        )

    def pinned_candidate_id(self) -> str:
        return self.kernel.current_catalog().candidates[0].candidate_id
