"""The one hardware-free deployment every vertical and fault test acts on.

Not a test module (``discover`` only collects ``test*.py``): it holds the real
contract set, the real Kernel, the real Write Gateway over the mock adapter,
and the real deterministic advisory agents, so each ``test_vertical_*.py`` and
``test_fault_*.py`` file can be read as a list of claims rather than as a
setup script.

Nothing here is a stub of a lane's own component.  The contracts go through
:func:`assurance.contracts.validation.validate_family_set`, the epoch through
:meth:`assurance.kernel.kernel.AssuranceKernel.freeze_epoch` (which runs
KCON's real ``generate_catalog`` and ``freeze_epoch``), the actuation through
:class:`assurance.gateway.gateway.TokenBoundWriteGateway`, and the
measurements through :class:`assurance.collector.mock_source.MockMeasurementSource`.
The only injected fakes are the deployment itself -- a dictionary of
configuration axes -- and the clock, which is what makes the run replayable.

Hardware-free by construction: no socket is opened, no process is started and
no model is called.  ``test_vertical_boundary.py`` asserts that rather than
trusting this sentence.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from assurance.advisors.coordinator import StrategyBackedEvidenceCoordinator
from assurance.advisors.deterministic_agents import (
    DeterministicIntentAgent,
    RuleBasedXAppAgent,
)
from assurance.advisors.grammar import IntentGrammarEntry
from assurance.advisors.strategies import DeterministicStrategy
from assurance.collector.mock_source import (
    MockMeasurementSource,
    clock_drift_source,
    gap_source,
    normal_timeseries_source,
    stale_source,
)
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
    contract_content_hash,
)
from assurance.core.axes import EvidenceCellStatus
from assurance.core.confirmation import ConfirmationAction, ConfirmationRecord
from assurance.core.envelopes import ASSURANCE_SCHEMA_VERSION
from assurance.core.provenance import Provenance, TypedQuantity
from assurance.gateway.gateway import TokenBoundWriteGateway
from assurance.gateway.journal import InMemoryTransactionJournal
from assurance.gateway.mock_adapter import FaultInjection, MockActuationAdapter
from assurance.gateway.plan import config_hash
from assurance.kernel.event_store import MemoryEventStore
from assurance.kernel.kernel import AssuranceKernel
from assurance.kernel.reducer import KernelReducer
from assurance.vertical import SteppingClock, VerticalDeployment, VerticalPath

START = "2026-08-21T09:00:00.000000Z"

#: The deployment's contracted configuration surface, at rest.
BASELINE_CONFIG: Mapping[str, Any] = {"queuePriority": 1, "servingCell": "cell-1"}

#: The contracted safe configuration.  Deliberately different from the
#: baseline, so a test that wants the two to coincide has to say so: an
#: emergency safe state that always equalled the baseline would hide the
#: difference between "recovered" and "safe but not where the trial started".
SAFE_STATE: Mapping[str, Any] = {"queuePriority": 0, "servingCell": "cell-1"}

#: A deployment whose contracted safe configuration *is* the resting
#: configuration.  The common shape for a steering objective: the safe thing
#: to do with a UE is to leave it where it was.
SAFE_STATE_IS_BASELINE: Mapping[str, Any] = dict(BASELINE_CONFIG)

SCOPE: Mapping[str, Any] = {"guAmfUeNgapId": "ue-1"}
ADAPTER = "mock"
CASE_ID = "case/steer-1"
CELL_ID = "cell/steer-c2"
#: The epoch freezes the *ordered target refs* as the vector order, so the
#: case's active vector is a target ref.
ACTIVE_VECTOR = "target/steer"
#: The *contract* counter id.  A RawSample carries the contract counter,
#: resolved through the CounterBinding; ``dl_throughput`` is the
#: deployment-side name and never appears on a sample.
COUNTER_ID = "counter/dl-throughput"
SAMPLE_SCOPE = {"cellId": "cell-1", "ueId": "ue-1"}

#: Window geometry.  Four samples one second apart fill exactly one 3 s
#: window; the trial is evaluated one further second later, inside the 2 s
#: freshness bound.
CADENCE_MS = 1000
WINDOW_MS = 3000
HOLD_MS = 3000
FRESHNESS_MS = 2000

IDENTITY = {
    "version": "1.0.0",
    "schema_version": ASSURANCE_SCHEMA_VERSION,
    "document_status": "NORMATIVE",
    "standard_mapping": {"a1": "1.0", "e2sm-rc": "1.0"},
}


def identity(contract_id: str) -> Dict[str, Any]:
    return {**IDENTITY, "contract_id": contract_id}


def measured(value: float, unit: str, source: str) -> TypedQuantity:
    return TypedQuantity(value, unit, Provenance.EXPERIMENT_CONFIG, source)


def contract_set(
    *, throughput_bound: float = 3.0, validity_bound: Optional[float] = None
) -> Dict[str, Any]:
    """The complete, cross-consistent contract family for the deployment."""
    counter = CounterBinding(
        counter_id="counter/dl-throughput",
        deployment_counter_name="dl_throughput",
        source=MeasurementSource.E2_KPM,
        scope_keys=("cellId", "ueId"),
        unit="Mbps",
        native_cadence_ms=CADENCE_MS,
        deployment_binding_ref="deployment/r1",
    )
    measurement = MeasurementContract(
        **identity("measurement/dl-throughput"),
        counter_id="counter/dl-throughput",
        scope_selector={"cellId": "cell-1"},
        membership_snapshot=("ue-1",),
        cadence_ms=CADENCE_MS,
        window_width_ms=WINDOW_MS,
        window_stride_ms=WINDOW_MS,
        overlap=OverlapPolicy.DISJOINT,
        aggregation=Aggregation.MEAN,
        estimator=Estimator.SAMPLE_MEAN,
        minimum_entity_count=1,
        hold_ms=HOLD_MS,
        gap_policy=GapPolicy.CONSERVATIVE_CHARGE,
        missing_interval_charge=measured(5.0, "ms", "plan/missing-charge"),
        freshness_bound_ms=FRESHNESS_MS,
        clock_requirement=ClockRequirement.SYNCHRONISED_REQUIRED,
        uncertainty_rule=UncertaintyRule(
            "bounded_absolute", measured(0.1, "Mbps", "calibration/dl-throughput")
        ),
    )
    predicate = TargetPredicate(
        "dl-throughput-floor",
        TypedConstraint(
            "measurement/dl-throughput",
            ComparisonOperator.GREATER_OR_EQUAL,
            measured(throughput_bound, "Mbps", "plan/target-floor"),
        ),
    )
    option = TargetOption(
        **identity("option/steer"),
        capability_ref="capability/steer",
        # The parameter space names configuration axes, so the candidate the
        # epoch freezes is exactly the change the plan will make.
        parameter_space={"queuePriority": ("7",), "servingCell": ("cell-2", "cell-3")},
    )
    # A validity-region constraint is a condition the *trial* must hold under
    # to mean anything; failing it is an execution-validity exit, not a KPI
    # failure (design section 8 keeps the two axes apart).
    validity_region = (
        (
            TypedConstraint(
                "measurement/dl-throughput",
                ComparisonOperator.GREATER_OR_EQUAL,
                measured(validity_bound, "Mbps", "plan/validity-floor"),
            ),
        )
        if validity_bound is not None
        else ()
    )
    target = TargetContract(
        **identity("target/steer"),
        objective_family="TrafficSteeringPreference",
        scope_selector={"cellId": "cell-1"},
        predicates=(predicate,),
        options=(option,),
        validity_region=validity_region,
        hold_ms=HOLD_MS,
    )
    vector = TargetVector(
        **identity("vector/steer"), ordered_target_refs=("target/steer",)
    )
    release = TargetReleasePolicy(**identity("release/steer"))
    case_policy = CoordinationCasePolicy(
        **identity("case-policy/steer"),
        deadline_ms=600_000,
        max_trials=3,
        max_proposals=6,
        target_release_policy_ref="release/steer",
        harm_contract_refs=("harm/steer",),
    )
    watchdog = WatchdogContract(
        **identity("watchdog/dl-throughput"),
        watchdog_id="wd/dl-throughput",
        trigger=TypedConstraint(
            "measurement/dl-throughput",
            ComparisonOperator.GREATER_OR_EQUAL,
            measured(1.0, "Mbps", "plan/watchdog-floor"),
        ),
        action=WatchdogAction.STOP_AND_ROLLBACK,
    )
    bound = CertifiedHarmBound(
        "bound/steer",
        measured(20.0, "ms", "calibration/steer-bound"),
        measured(2.0, "ms", "calibration/steer-uncertainty"),
        measured(22.0, "ms", "calibration/steer-conservative"),
        "measurement/dl-throughput#uncertainty",
        {"cellId": "cell-1"},
        10_000,
        ("calibration/steer-1",),
        "proof/steer-v1",
    )
    harm = HarmContract(
        **identity("harm/steer"),
        harm_kind=HarmKind.TRIAL_INDUCED,
        scope_selector={"cellId": "cell-1"},
        reserve=measured(100.0, "ms", "plan/steer-reserve"),
        bounds=(bound,),
        watchdogs=(watchdog,),
        missing_interval_charge=measured(5.0, "ms", "plan/missing-charge"),
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
        **identity("actuator/steer"),
        capability_ref="capability/steer",
        path=ActuatorPath.OFFICIAL_ORAN_DYNAMIC,
        policy_type_id="20008",
        service_model={"serviceModel": "E2SM-RC", "style": "3"},
        readback_measurement_ref="measurement/dl-throughput",
        deployment_binding_ref="deployment/r1",
    )
    capability = CapabilityManifest(
        **identity("capability/steer"),
        capability_id="capability/steer",
        supported_objectives=("TrafficSteeringPreference",),
        constraints=(predicate.constraint,),
        actuator_refs=("actuator/steer",),
        measurement_refs=("measurement/dl-throughput",),
        interface_versions={"e2smRc": "1.0", "a1p": "1.0"},
    )
    composition = CompositionManifest(
        **identity("composition/lab"),
        composition_id="composition/lab",
        capability_refs=("capability/steer",),
    )
    return {
        "counter": counter,
        "measurement": measurement,
        "target": target,
        "option": option,
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


def confirmation_for(contract: Any, *, event_id: str, at: str = START) -> ConfirmationRecord:
    """The Operator confirmation of one content-addressed object.

    Content hash and instant only: design section 5 removes signer identity
    from the system, so a confirmation records *what* was confirmed and when,
    never who pressed the button.
    """
    return ConfirmationRecord(
        confirmed_object_type=type(contract).__name__,
        confirmed_content_hash=contract_content_hash(contract),
        event_id=event_id,
        timestamp=at,
        action=ConfirmationAction.CONFIRM_AND_START,
    )


INTENT_GRAMMAR: Mapping[str, IntentGrammarEntry] = {
    "TrafficSteeringPreference": IntentGrammarEntry(
        objective_family="TrafficSteeringPreference",
        keywords=("steer", "throughput", "cell"),
        measurement_ref="measurement/dl-throughput",
        default_operator=ComparisonOperator.GREATER_OR_EQUAL,
        default_unit="Mbps",
    ),
}


class VerticalFixture:
    """Mixin building the whole vertical path for one test."""

    def build(
        self,
        *,
        faults: Optional[FaultInjection] = None,
        config: Optional[Mapping[str, Any]] = None,
        throughput_bound: float = 3.0,
        validity_bound: Optional[float] = None,
        safe_state: Optional[Mapping[str, Any]] = None,
        adapter_hosts_watchdogs: bool = True,
        with_coordinator: bool = True,
        collector: Optional[Any] = None,
        journal: Optional[Any] = None,
        store: Optional[MemoryEventStore] = None,
        clock: Optional[SteppingClock] = None,
        evidence_cells: Optional[Sequence[EvidenceCell]] = None,
        max_trials: Optional[int] = None,
        strategy: Optional[Any] = None,
    ) -> VerticalPath:
        self.clock = clock or SteppingClock(START)
        self.contracts = contract_set(
            throughput_bound=throughput_bound, validity_bound=validity_bound
        )
        self.adapter = MockActuationAdapter(
            config=dict(config if config is not None else BASELINE_CONFIG),
            faults=faults,
        )
        # A deployment that cannot host a contract watchdog -- the shape of
        # the A1 policy path, where the guard lives inside the policy body and
        # so does not exist until the policy is created.
        self.adapter.hosts_watchdogs = adapter_hosts_watchdogs
        self.journal = journal if journal is not None else InMemoryTransactionJournal()
        self.safe_state = dict(safe_state if safe_state is not None else SAFE_STATE)
        self.gateway = TokenBoundWriteGateway(
            adapters={ADAPTER: self.adapter},
            safe_state=self.safe_state,
            journal=self.journal,
            clock=self.clock,
        )
        self.store = store if store is not None else MemoryEventStore()
        self.kernel = AssuranceKernel(
            event_store=self.store,
            reducer=KernelReducer(),
            write_gateway=self.gateway,
            measurement_collector=None,
        )
        self.epoch = self.admit_and_freeze(max_trials=max_trials)
        self.open_case(evidence_cells=evidence_cells)
        self.collector = (
            collector
            if collector is not None
            else normal_timeseries_source(
                counter_id=COUNTER_ID,
                scope=SAMPLE_SCOPE,
                start=self.clock(),
                cadence_ms=CADENCE_MS,
                count=5,
                value=4.0,
            )
        )
        self.intent_agent = DeterministicIntentAgent()
        self.xapp_agent = RuleBasedXAppAgent()
        # *strategy* lets a test run the identical deployment behind any of
        # design section 12's six comparable strategies.  Default unchanged:
        # every existing test still runs behind DETERMINISTIC.
        self.coordinator = (
            StrategyBackedEvidenceCoordinator(
                strategy=strategy if strategy is not None else DeterministicStrategy()
            )
            if with_coordinator
            else None
        )
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
            coordinator=self.coordinator,
            collector=self.collector,
        )
        return self.path

    # -- admission and freeze ---------------------------------------------

    def admit_and_freeze(self, *, max_trials: Optional[int] = None) -> Any:
        data = self.contracts
        if max_trials is not None:
            from dataclasses import replace

            data["case_policy"] = replace(data["case_policy"], max_trials=max_trials)
        now = self.clock()
        self.vector_confirmation = confirmation_for(
            data["vector"], event_id="confirm/vector-1", at=now
        )
        policy_confirmation = confirmation_for(
            data["case_policy"], event_id="confirm/policy-1", at=now
        )
        for key in (
            "counter",
            "measurement",
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
            data["vector"], confirmation=self.vector_confirmation, now=now
        )
        self.kernel.admit_contract(
            data["case_policy"], confirmation=policy_confirmation, now=now
        )
        self.kernel.admit_deployment(data["deployment"], now=now)
        return self.kernel.freeze_epoch(confirmation=self.vector_confirmation, now=now)

    def open_case(self, *, evidence_cells: Optional[Sequence[EvidenceCell]] = None) -> None:
        catalog = self.kernel.current_catalog()
        cells = evidence_cells
        if cells is None:
            cells = (
                EvidenceCell(
                    cell_id=CELL_ID,
                    target_ref="target/steer",
                    candidate_semantic_hash=catalog.candidates[0].semantic_hash,
                    status=EvidenceCellStatus.OPEN,
                    required_independent_contributions=1,
                ),
            )
        self.kernel.open_case(
            case_id=CASE_ID,
            policy=self.contracts["case_policy"],
            active_vector=ACTIVE_VECTOR,
            usable_reserve={
                "harm/steer": measured(100.0, "ms", "plan/steer-reserve").to_canonical_dict()
            },
            reserve_per_trial={
                "harm/steer": measured(20.0, "ms", "plan/steer-per-trial").to_canonical_dict()
            },
            evidence_cells=tuple(cells),
            now=self.clock(),
        )

    # -- convenience -------------------------------------------------------

    def first_candidate_id(self) -> str:
        return self.kernel.current_catalog().candidates[0].candidate_id

    def applied_config_for(self, candidate_index: int = 0) -> Dict[str, Any]:
        candidate = self.kernel.current_catalog().candidates[candidate_index]
        merged = dict(BASELINE_CONFIG)
        merged.update(dict(candidate.parameters))
        return merged

    def baseline_hash(self) -> str:
        return config_hash(BASELINE_CONFIG)

    def safe_state_hash(self) -> str:
        return config_hash(self.safe_state)


def failing_collector(kind: str, *, start: str, value: float = 4.0) -> MockMeasurementSource:
    """One of the four scripted measurement pathologies (design section 4.5)."""
    builders = {
        "gap": lambda: gap_source(
            counter_id=COUNTER_ID,
            scope=SAMPLE_SCOPE,
            start=start,
            cadence_ms=CADENCE_MS,
            count=5,
            gap_at=2,
            value=value,
            unit="Mbps",
        ),
        "clock-drift": lambda: clock_drift_source(
            counter_id=COUNTER_ID,
            scope=SAMPLE_SCOPE,
            start=start,
            cadence_ms=CADENCE_MS,
            count=5,
            drift_at=2,
            value=value,
            unit="Mbps",
        ),
        "stale": lambda: stale_source(
            counter_id=COUNTER_ID,
            scope=SAMPLE_SCOPE,
            observed_at=start,
            cadence_ms=CADENCE_MS,
            value=value,
            unit="Mbps",
        ),
        "silent": lambda: MockMeasurementSource(script=[], scope=SAMPLE_SCOPE),
    }
    return builders[kind]()


def timeseries(*, start: str, value: float, count: int = 5) -> MockMeasurementSource:
    return normal_timeseries_source(
        counter_id=COUNTER_ID,
        scope=SAMPLE_SCOPE,
        start=start,
        cadence_ms=CADENCE_MS,
        count=count,
        value=value,
        unit="Mbps",
    )
