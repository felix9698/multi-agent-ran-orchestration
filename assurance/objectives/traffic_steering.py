"""``TrafficSteeringPreference`` — objective family module.

Owner lane: **OBJ1** (``docs/architecture/SEAMS-GATE4.md`` section 3).

The registry record for this family is
``assurance.objectives.registry.record_for("TrafficSteeringPreference")``; read
it before filling a seat below, because it is what the family must stay honest
against.  Two entries in it constrain this module directly:

* the actuation and readback path is the one Gate 3 proved on real equipment --
  A1-P v2 carrying the frozen project policy type, E2SM-RC 1.03 Style 3 Action
  1 with ``targetPrimaryCellId``, read back through E2SM-KPM 2.03 UE-level
  attribution -- so :meth:`contract_bundle` builds against that path and no
  other;
* the frozen deployment advertises the objective kind ``PIN_TO_CELL`` alone, so
  this family is not submittable today.  Establishing the versioned mapping
  that would change that is OBJ1's decision and belongs in
  ``docs/architecture/SEAMS-GATE4.md`` section 6 together with the registry
  edit -- not in a quiet widening of the allowlist.

``PIN_TO_CELL`` stays exactly where it is.  Task section 8 keeps it as an exact
regression contract under its own identifier, and
``tests/assurance/pin_to_cell_support.py`` is not this lane's file to edit.
"""

from __future__ import annotations
from assurance.contracts.actuation_request import enforced_timeout_ms_default

from typing import Any, Mapping, Tuple

from assurance.contracts.capability import (
    ActuatorBinding, ActuatorPath, CapabilityManifest, CompositionManifest,
    DeploymentBinding,
)
from assurance.contracts.catalog import CoordinationCasePolicy
from assurance.contracts.harm import (
    CertifiedHarmBound, HarmContract, HarmKind, WatchdogAction, WatchdogContract,
)
from assurance.contracts.measurement import (
    Aggregation, ClockRequirement, CounterBinding, Estimator, GapPolicy,
    MeasurementContract, MeasurementSource, OverlapPolicy, UncertaintyRule,
)
from assurance.contracts.target import (
    ComparisonOperator, TargetContract, TargetOption, TargetPredicate,
    TargetReleasePolicy, TargetVector, TypedConstraint,
)
from assurance.core.axes import EvidenceCellStatus, TrialOutcome
from assurance.core.envelopes import ASSURANCE_SCHEMA_VERSION
from assurance.core.provenance import Provenance, TypedQuantity
from assurance.core.states import StopReason, TrialState
from assurance.objectives.family import (
    ExpectedConfiguration, ExpectedOutcome, KpiUse,
    KpiDeclaration,
    ObjectiveContractBundle,
    ObjectiveFamilyModule,
    PolicyLifecycle,
    ScenarioName,
)

__all__ = ["TrafficSteeringPreferenceFamily"]


_HOME_NCI = 12345678
_TARGET_NCI = 87654321


def _identity(contract_id: str) -> Mapping[str, Any]:
    return {
        "contract_id": contract_id,
        "version": "1.0.0",
        "schema_version": ASSURANCE_SCHEMA_VERSION,
        "document_status": "NORMATIVE",
        "standard_mapping": {"a1p": "2", "e2sm-rc": "1.03", "e2sm-kpm": "2.03"},
    }


def _quantity(value: float, unit: str, source: str) -> TypedQuantity:
    return TypedQuantity(value, unit, Provenance.EXPERIMENT_CONFIG, source)


def _oracle(evaluation: Mapping[str, Any]) -> TrialOutcome:
    """Return the outcome already written by the Kernel; do not re-judge KPIs."""
    value = evaluation.get("trialOutcome", evaluation.get("outcome"))
    if isinstance(value, TrialOutcome):
        return value
    if value is not None:
        return TrialOutcome(value)
    return TrialOutcome.NOT_SETTLED


def _expectations() -> Mapping[ScenarioName, ExpectedOutcome]:
    return {
        ScenarioName.POSITIVE: ExpectedOutcome(
            TrialState.SETTLED_SUCCESS, TrialOutcome.SUCCESS,
            evidence_status=EvidenceCellStatus.CLOSED_PASS.value,
            configuration=ExpectedConfiguration.APPLIED,
            rationale="all mandatory predicates passed in the Kernel's completed hold",
        ),
        ScenarioName.NEGATIVE: ExpectedOutcome(
            TrialState.SETTLED_NON_SUCCESS, TrialOutcome.FAIL,
            stop_reason=StopReason.SEMANTIC_NON_SUCCESS,
            evidence_status=EvidenceCellStatus.CLOSED_FAIL.value,
            rationale="a sufficient measured predicate miss is a semantic non-success",
        ),
        ScenarioName.STALE: ExpectedOutcome(
            TrialState.SETTLED_NON_SUCCESS, TrialOutcome.SAFETY_STOPPED,
            stop_reason=StopReason.TELEMETRY_STALE,
            evidence_status=EvidenceCellStatus.OPEN.value,
            rationale="stale telemetry stops safely without deciding the evidence cell",
        ),
        ScenarioName.MISSING: ExpectedOutcome(
            TrialState.SETTLED_NON_SUCCESS, TrialOutcome.INDETERMINATE,
            stop_reason=StopReason.SEMANTIC_NON_SUCCESS,
            rationale="missing observations leave the required predicate undecidable",
        ),
        ScenarioName.PARTIAL_EFFECT: ExpectedOutcome(
            TrialState.SETTLED_NON_SUCCESS, TrialOutcome.SAFETY_STOPPED,
            rationale="a partial configuration application is reversed before judgement",
        ),
        ScenarioName.FAULT: ExpectedOutcome(
            TrialState.SETTLED_NON_SUCCESS, TrialOutcome.EXEC_ERROR,
            rationale="a prepare fault reaches no effect and is recorded as execution error",
        ),
    }


def _bundle(
    *, family: str, scope: Mapping[str, Any], deployment: DeploymentBinding,
    qos: bool, steering: bool, qoe: bool = False,
) -> ObjectiveContractBundle:
    """Build OBJ1 contracts around the released cell-steering actuator.

    ``qos`` and ``steering`` select mandatory predicate sets, not actuator
    implementations.  Every OBJ1 family is expressed through the one released
    actuator: AIC UE cell steering over E2SM-RC Style 3 / Action 1.  A QoS
    family therefore keeps QoS measurements and predicates while changing the
    selected cell; it does not claim a slice-PRB actuator exists in this gate.
    """
    ue_id = str(scope.get("ueId", "ue-1"))
    cell_id = str(scope.get("cellId", "NRCellDU-1"))
    scope_selector = {"ueId": ue_id, "cellId": cell_id}
    # A steering-only family (TrafficSteeringPreference) keeps its cell pair fixed
    # in module constants so the sentence and the frozen contract never disagree.
    # A QoS family selects whichever cell meets the quality floor, so it honours
    # the live scope's observed home and requested target and can steer in either
    # direction -- the objective is the floor, not a hard-coded destination.
    if qos:
        home_nci = int(scope.get("homeServingCell", _HOME_NCI))
        target_nci = int(scope.get("targetServingCell", _TARGET_NCI))
    else:
        home_nci, target_nci = _HOME_NCI, _TARGET_NCI
    counters = []
    measurements = []
    predicates = []
    membership = (ue_id,)
    evaluation_cadence_ms = 60000 if qos else 1000
    evaluation_window_ms = 120000 if qos else 3000
    hold_ms = 120000 if qos else 3000

    serving_counter_id = "counter/serving-cell"
    counters.append(CounterBinding(
        counter_id=serving_counter_id,
        deployment_counter_name="UE.ServingCell",
        source=MeasurementSource.CONFIGURATION_READBACK,
        scope_keys=("ueId",),
        unit="nci",
        native_cadence_ms=1000,
        deployment_binding_ref=deployment.contract_id,
    ))
    for aggregation, suffix, description in (
        (Aggregation.MIN, "min", "no sample observed a lower serving-cell identity"),
        (Aggregation.MAX, "max", "no sample observed a higher serving-cell identity"),
    ):
        measurement_id = f"measurement/{family}/serving-cell-{suffix}"
        measurements.append(MeasurementContract(
            **_identity(measurement_id),
            counter_id=serving_counter_id,
            scope_selector={"ueId": ue_id},
            membership_snapshot=membership,
            # Actuation confirmation reads serving-cell-min inside the R1
            # action deadline.  Keep identity on its delivered one-second
            # cadence; only the QoS quality counters use the 60-second grid.
            cadence_ms=1000,
            window_width_ms=evaluation_window_ms,
            window_stride_ms=evaluation_window_ms,
            overlap=OverlapPolicy.DISJOINT,
            aggregation=aggregation,
            estimator=Estimator.EMPIRICAL_QUANTILE,
            minimum_entity_count=1,
            hold_ms=hold_ms,
            gap_policy=GapPolicy.CONSERVATIVE_CHARGE,
            missing_interval_charge=_quantity(5, "ms", "obj1/missing"),
            freshness_bound_ms=2000,
            clock_requirement=ClockRequirement.SYNCHRONISED_REQUIRED,
            uncertainty_rule=UncertaintyRule(
                "exact_identity", _quantity(0, "nci", "obj1/identity")
            ),
        ))
        if steering:
            predicates.append(TargetPredicate(
                f"serving-cell-preferred-{suffix}",
                TypedConstraint(
                    measurement_id,
                    ComparisonOperator.EQUAL,
                    _quantity(float(target_nci), "nci", "obj1/preferred-cell"),
                ),
                description=description,
            ))

    if qos:
        prb_counter_id = "counter/rru-prb-dl"
        prb_measurement_id = f"measurement/{family}/dl-prb"
        counters.append(CounterBinding(
            counter_id=prb_counter_id,
            deployment_counter_name="RRU.PrbDl",
            source=MeasurementSource.O1_PM,
            scope_keys=("cellId",),
            unit="percent",
            native_cadence_ms=60000,
            deployment_binding_ref=deployment.contract_id,
        ))
        measurements.append(MeasurementContract(
            **_identity(prb_measurement_id),
            counter_id=prb_counter_id,
            scope_selector={"cellId": cell_id},
            membership_snapshot=membership,
            cadence_ms=60000,
            window_width_ms=120000,
            window_stride_ms=120000,
            overlap=OverlapPolicy.DISJOINT,
            aggregation=Aggregation.MEAN,
            estimator=Estimator.SAMPLE_MEAN,
            minimum_entity_count=1,
            hold_ms=120000,
            gap_policy=GapPolicy.CONSERVATIVE_CHARGE,
            missing_interval_charge=_quantity(5, "ms", "obj1/missing"),
            freshness_bound_ms=60000,
            clock_requirement=ClockRequirement.SYNCHRONISED_REQUIRED,
            uncertainty_rule=UncertaintyRule(
                "bounded_absolute",
                _quantity(0.1, "percent", "obj1/prb-calibration"),
            ),
        ))
        predicates.append(TargetPredicate(
            "dl-prb-headroom",
            TypedConstraint(
                prb_measurement_id,
                ComparisonOperator.LESS_OR_EQUAL,
                _quantity(50, "percent", "obj1/prb-headroom"),
            ),
            description="delivered cell-scope RRU.PrbDl remains within the agreed headroom",
        ))

    if qoe:
        qoe_counter_id = "counter/application-qoe-score"
        qoe_measurement_id = f"measurement/{family}/application-experience"
        counters.append(CounterBinding(
            counter_id=qoe_counter_id,
            deployment_counter_name="APP.QoEScore",
            source=MeasurementSource.CONFIGURATION_READBACK,
            scope_keys=("ueId",),
            unit="qoe-score",
            native_cadence_ms=1000,
            deployment_binding_ref=deployment.contract_id,
        ))
        measurements.append(MeasurementContract(
            **_identity(qoe_measurement_id), counter_id=qoe_counter_id,
            scope_selector={"ueId": ue_id}, membership_snapshot=membership,
            cadence_ms=1000, window_width_ms=3000, window_stride_ms=3000,
            overlap=OverlapPolicy.DISJOINT, aggregation=Aggregation.MEAN,
            estimator=Estimator.SAMPLE_MEAN, minimum_entity_count=1,
            hold_ms=3000, gap_policy=GapPolicy.CONSERVATIVE_CHARGE,
            missing_interval_charge=_quantity(5, "ms", "obj2/qoe-missing"),
            freshness_bound_ms=2000,
            clock_requirement=ClockRequirement.SYNCHRONISED_REQUIRED,
            uncertainty_rule=UncertaintyRule(
                "bounded_absolute", _quantity(0.02, "qoe-score", "obj2/qoe-formula-v1")
            ),
        ))
        predicates.append(TargetPredicate(
            "application-experience-floor",
            TypedConstraint(
                qoe_measurement_id, ComparisonOperator.GREATER_OR_EQUAL,
                _quantity(0.70, "qoe-score", "obj2/qoe-floor"),
            ),
            description=(
                "episode QoE score derived from UE-observed delivered DL goodput, "
                "one-way latency, jitter and loss remains above the agreed floor"
            ),
        ))

    if qos:
        throughput_counter_id = "counter/kpm-f3-drb-ue-thp-dl"
        throughput_measurement_id = f"measurement/{family}/ue-throughput"
        counters.append(CounterBinding(
            counter_id=throughput_counter_id,
            deployment_counter_name="DRB.UEThpDl",
            source=MeasurementSource.E2_KPM,
            scope_keys=("ueId",),
            unit="kbit/s",
            native_cadence_ms=1000,
            deployment_binding_ref=deployment.contract_id,
        ))
        measurements.append(MeasurementContract(
            **_identity(throughput_measurement_id),
            counter_id=throughput_counter_id,
            scope_selector={"ueId": ue_id},
            membership_snapshot=membership,
            cadence_ms=60000,
            window_width_ms=120000,
            window_stride_ms=120000,
            overlap=OverlapPolicy.DISJOINT,
            aggregation=Aggregation.MEAN,
            estimator=Estimator.SAMPLE_MEAN,
            minimum_entity_count=1,
            hold_ms=120000,
            gap_policy=GapPolicy.CONSERVATIVE_CHARGE,
            missing_interval_charge=_quantity(5, "ms", "obj1/missing"),
            freshness_bound_ms=60000,
            clock_requirement=ClockRequirement.SYNCHRONISED_REQUIRED,
            uncertainty_rule=UncertaintyRule(
                "bounded_absolute",
                _quantity(1, "kbit/s", "obj1/kpm-f3-calibration"),
            ),
        ))
        predicates.append(TargetPredicate(
            "ue-throughput-floor",
            TypedConstraint(
                throughput_measurement_id,
                ComparisonOperator.GREATER_OR_EQUAL,
                _quantity(500, "kbit/s", "obj1/ue-throughput-floor"),
            ),
            description=(
                "the delivered KPM Format 3 per-UE DRB.UEThpDl counter remains "
                "above the agreed floor"
            ),
        ))

    capability_id = f"capability/{family}/steering"
    actuator_id = f"actuator/{family}/steering"
    actuator = ActuatorBinding(
        **_identity(actuator_id),
        capability_ref=capability_id,
        path=ActuatorPath.OFFICIAL_ORAN_DYNAMIC,
        policy_type_id="AIC_UECellSteering_1.0.0",
        service_model={
            "serviceModel": "E2SM-RC",
            "style": "3",
            "action": "1",
            "profile": "E2SM-RC-STYLE3-ACTION1",
        },
        readback_measurement_ref=f"measurement/{family}/serving-cell-min",
        deployment_binding_ref=deployment.contract_id,
    )
    capability = CapabilityManifest(
        **_identity(capability_id),
        capability_id=capability_id,
        supported_objectives=(family,),
        constraints=tuple(predicate.constraint for predicate in predicates),
        actuator_refs=(actuator_id,),
        measurement_refs=tuple(
            measurement.contract_id for measurement in measurements
        ),
        interface_versions={
            "a1p": "2",
            "e2smRc": "1.03",
            "e2smKpm": "2.03",
            **({"o1": "3GPP-TS-32.435-V10.0"} if qos else {}),
            **({"applicationExperience": "APP-QOE-EPISODE-v1"} if qoe else {}),
        },
    )
    option = TargetOption(
        **_identity(f"option/{family}/steering"),
        capability_ref=capability_id,
        parameter_space={"servingCell": (str(target_nci),)},
    )

    target_id = f"target/{family}"
    target = TargetContract(
        **_identity(target_id), objective_family=family, scope_selector=scope_selector,
        predicates=tuple(predicates), options=(option,), hold_ms=hold_ms,
    )
    vector = TargetVector(**_identity(f"vector/{family}"), ordered_target_refs=(target_id,))
    release = TargetReleasePolicy(**_identity(f"release/{family}"))
    harm_id = f"harm/{family}"
    watchdog = WatchdogContract(
        **_identity(f"watchdog/{family}"), watchdog_id=f"wd/{family}",
        trigger=predicates[0].constraint, action=WatchdogAction.STOP_AND_ROLLBACK,
    )
    bound = CertifiedHarmBound(
        f"bound/{family}", _quantity(20, "ms", "obj1/calibration"),
        _quantity(2, "ms", "obj1/calibration"), _quantity(22, "ms", "obj1/calibration"),
        f"{measurements[0].contract_id}#uncertainty", scope_selector,
        enforced_timeout_ms_default(),
        ("calibration/obj1",), f"proof/{family}",
    )
    harm = HarmContract(
        **_identity(harm_id), harm_kind=HarmKind.TRIAL_INDUCED, scope_selector=scope_selector,
        reserve=_quantity(100, "ms", "obj1/reserve"), bounds=(bound,), watchdogs=(watchdog,),
        missing_interval_charge=_quantity(5, "ms", "obj1/missing"),
    )
    case_policy = CoordinationCasePolicy(
        **_identity(f"case-policy/{family}"), deadline_ms=600000, max_trials=3, max_proposals=6,
        target_release_policy_ref=release.contract_id, harm_contract_refs=(harm_id,),
    )
    composition = CompositionManifest(
        **_identity(f"composition/{family}"), composition_id=f"composition/{family}",
        capability_refs=(capability.contract_id,),
    )
    return ObjectiveContractBundle(
        family=family, counters=tuple(counters), measurements=tuple(measurements), target=target,
        vector=vector, release=release, case_policy=case_policy, watchdogs=(watchdog,), harm=harm,
        deployment=deployment, actuators=(actuator,), capabilities=(capability,), composition=composition,
        baseline_config={"servingCell": str(home_nci)},
        safe_state={"servingCell": str(home_nci)},
        scope=scope_selector, sample_scope=scope_selector,
    )


class TrafficSteeringPreferenceFamily(ObjectiveFamilyModule):
    """Seats frozen by ``docs/architecture/SEAMS-GATE4.md``; bodies owned by OBJ1."""

    family = "TrafficSteeringPreference"
    lane = "OBJ1"

    def contract_bundle(
        self, *, scope: Mapping[str, Any], deployment_binding: DeploymentBinding
    ) -> ObjectiveContractBundle:
        """Target/Harm/Measurement set for a steering preference over one scope.

        Body owned by lane OBJ1 (``docs/architecture/SEAMS-GATE4.md``).  The
        mandatory predicate is a serving-cell identity condition held across
        every completed window, not an end-of-episode readback.
        """
        return _bundle(family=self.family, scope=scope, deployment=deployment_binding, qos=False, steering=True)

    def candidate_parameters(self) -> Mapping[str, Tuple[str, ...]]:
        """The finite set of preferred cells, as configuration-axis settings.

        Body owned by lane OBJ1 (``docs/architecture/SEAMS-GATE4.md``).
        """
        return {"servingCell": (str(_TARGET_NCI),)}

    def policy_lifecycle(self) -> PolicyLifecycle:
        """R1 request/response/status and A1-P policy lifecycle for this family.

        Body owned by lane OBJ1 (``docs/architecture/SEAMS-GATE4.md``).
        """
        return PolicyLifecycle("AIC_UECellSteering_1.0.0", ("R1_REQUESTED", "A1_POLICY_CREATED", "E2_CONTROL_ACKNOWLEDGED", "READBACK_ENFORCED", "R1_STATUS"), "content hash plus R1 idempotency key", "corroborated UE.ServingCell readback through the completed hold", "The frozen deployment still refuses this project family before any R1 call.")

    def kpi_declaration(self) -> Tuple[KpiDeclaration, ...]:
        """Decision and assurance KPIs, with scope, unit, cadence and freshness.

        Body owned by lane OBJ1 (``docs/architecture/SEAMS-GATE4.md``).
        """
        return (
            KpiDeclaration("measurement/TrafficSteeringPreference/serving-cell-min", KpiUse.DECISION, "UE", "nci", 1000, 2000, "E2SM-KPM 2.03", "UE-level attribution"),
            KpiDeclaration("measurement/TrafficSteeringPreference/serving-cell-min", KpiUse.ASSURANCE, "UE", "nci", 1000, 2000, "E2SM-KPM 2.03", "minimum identity across each hold window"),
            KpiDeclaration("measurement/TrafficSteeringPreference/serving-cell-max", KpiUse.ASSURANCE, "UE", "nci", 1000, 2000, "E2SM-KPM 2.03", "maximum identity across each hold window"),
        )

    def terminal_oracle(self, evaluation: Mapping[str, Any]) -> TrialOutcome:
        """Name the terminal outcome for this family from the Kernel's axes.

        Body owned by lane OBJ1 (``docs/architecture/SEAMS-GATE4.md``).
        """
        return _oracle(evaluation)

    def hardware_free_expectations(self) -> Mapping[ScenarioName, ExpectedOutcome]:
        """What each verdict-bearing scenario of the shared matrix must end as.

        Body owned by lane OBJ1 (``docs/architecture/SEAMS-GATE4.md``).
        """
        return _expectations()
