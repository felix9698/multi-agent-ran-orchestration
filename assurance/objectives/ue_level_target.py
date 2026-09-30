"""``UELevelTarget`` — objective family module.

Owner lane: **OBJ2** (``docs/architecture/SEAMS-GATE4.md`` section 3).

Per-UE identification works here: E2SM-KPM UE-level attribution carried a
UE's records to one E2 node throughout Gate 3's OTA run.  Per-UE
*performance* does not.  The only per-UE quantity the capability manifest
declares is the serving cell identity; per-UE ``RRU.PrbTotDl`` is declared
by no capability entry, has no deployment registry entry, and the integration
constraints forbid summing, renaming or aliasing it onto cell-scope
``RRU.PrbDl``.

So a per-UE rate target has no source in this deployment and must fail
closed rather than be judged against a cell aggregate wearing a UE
label.  SEAMS-GATE4 section 9 leaves the judgable per-UE quantity as an
OPEN_QUESTION for this lane to settle; the settlement is that the target
predicate is an *identity* condition -- "this UE's serving cell is exactly
the stated one, for every sample of the hold" -- using the same MIN/MAX
identity-pair pattern ``tests/assurance/pin_to_cell_support.py`` already
validates for the preserved ``PIN_TO_CELL`` regression contract.  That
module is read here for the pattern and is not imported: this family is a
project-distinct objective, not a rename, and it builds its own contract
set through the frozen seat below rather than reusing that fixture's.

The registry record for this family is
``assurance.objectives.registry.record_for("UELevelTarget")``.  It is the honest
statement of what this deployment can and cannot do for this objective, and a
seat below may not be filled in a way that contradicts it: changing what the
family claims means editing the record and the SEAMS document in the same
change, with the evidence that justifies it.
"""

from __future__ import annotations
from assurance.contracts.actuation_request import enforced_timeout_ms_default

from typing import Any, Dict, Mapping, Tuple

from assurance.contracts.capability import (
    ActuatorBinding,
    ActuatorPath,
    CapabilityManifest,
    CompositionManifest,
    DeploymentBinding,
)
from assurance.contracts.catalog import CoordinationCasePolicy
from assurance.contracts.harm import (
    CertifiedHarmBound,
    HarmContract,
    HarmKind,
    WatchdogAction,
    WatchdogContract,
)
from assurance.contracts.measurement import (
    Aggregation,
    ClockRequirement,
    CounterBinding,
    Estimator,
    GapPolicy,
    MeasurementContract,
    MeasurementSource,
    OverlapPolicy,
    UncertaintyRule,
)
from assurance.contracts.target import (
    ComparisonOperator,
    TargetContract,
    TargetOption,
    TargetPredicate,
    TargetReleasePolicy,
    TargetVector,
    TypedConstraint,
)
from assurance.core.axes import (
    ExecutionValidity,
    MeasurementSufficiency,
    PredicateVerdict,
    TrialOutcome,
)
from assurance.core.envelopes import ASSURANCE_SCHEMA_VERSION
from assurance.core.provenance import Provenance, TypedQuantity
from assurance.objectives.family import (
    ExpectedConfiguration,
    ExpectedOutcome,
    KpiDeclaration,
    KpiUse,
    ObjectiveContractBundle,
    ObjectiveFamilyModule,
    PolicyLifecycle,
    ScenarioName,
)
from assurance.core.states import StopReason

__all__ = ["UELevelTargetFamily"]

#: The one A1 policy type this frozen deployment exposes.  UELevelTarget has
#: no policy type of its own -- the registry records this family as
#: unsubmittable because the type advertises objective kind ``PIN_TO_CELL``
#: only -- but the identity contract still actuates through the same real
#: E2SM-RC Style 3 Action 1 path Gate 3 exercised on equipment, so the
#: contract set below names the same frozen id rather than inventing one.
POLICY_TYPE_ID = "AIC_UECellSteering_1.0.0"

#: ``CId.ncI`` values for the two cells Gate 3's OTA run actually used.  Real
#: deployment facts, not fixture-only inventions: the same values appear in
#: ``config.py`` and the retained Gate 3 evidence.
HOME_NCI = 12345678
TARGET_NCI = 87654321

#: ``CId.ncI`` is a count of nothing; this unit token says what the number is
#: an identity of rather than pretending it is dimensionless.
NCI_UNIT = "nci"

COUNTER_ID = "counter/ue-level-serving-cell"
CADENCE_MS = 1000
WINDOW_MS = 3000
HOLD_MS = 3000
FRESHNESS_MS = 2000

IDENTITY = {
    "version": "1.0.0",
    "schema_version": ASSURANCE_SCHEMA_VERSION,
    "document_status": "NORMATIVE",
    "standard_mapping": {"a1p": "2.0", "e2sm-rc": "1.03", "e2sm-kpm": "2.03"},
}


def _identity(contract_id: str) -> Dict[str, Any]:
    return {**IDENTITY, "contract_id": contract_id}


def _quantity(value: float, unit: str, source: str) -> TypedQuantity:
    return TypedQuantity(value, unit, Provenance.EXPERIMENT_CONFIG, source)


def _serving_cell_measurement(
    contract_id: str, *, aggregation: Aggregation, ue_id: str = "ue-1"
) -> MeasurementContract:
    """One end of the per-UE identity predicate pair.

    Structurally the pattern ``tests/assurance/pin_to_cell_support.py`` already
    validates: MIN and MAX of the same counter, disjoint windows, an exact
    zero uncertainty because a cell identity has no measurement error.  Built
    fresh here rather than imported, because this family's contract ids,
    scope and counter are its own.

    ``ue_id`` is the UE the caller scoped the bundle to.  It has to reach the
    measurement contract, not just the target: the evaluator matches a sample's
    ``scopeSnapshot`` against this ``scope_selector`` and against
    ``membership_snapshot``, so a contract that named a different UE than the
    collector stamps would report ``SCOPE_MISMATCH`` and every predicate would
    come back ``INDETERMINATE`` -- with nothing in the trace saying why.  It
    defaulted to the fixture's ``ue-1`` before Gate 5 built a bundle over a live
    ``amfUeNgapId``, where the mismatch is what surfaced it.
    """
    return MeasurementContract(
        **_identity(contract_id),
        counter_id=COUNTER_ID,
        scope_selector={"ueId": ue_id},
        membership_snapshot=(ue_id,),
        cadence_ms=CADENCE_MS,
        window_width_ms=WINDOW_MS,
        window_stride_ms=WINDOW_MS,
        overlap=OverlapPolicy.DISJOINT,
        aggregation=aggregation,
        estimator=Estimator.EMPIRICAL_QUANTILE,
        minimum_entity_count=1,
        hold_ms=HOLD_MS,
        gap_policy=GapPolicy.CONSERVATIVE_CHARGE,
        missing_interval_charge=_quantity(5.0, "ms", "plan/ue-level-missing-charge"),
        freshness_bound_ms=FRESHNESS_MS,
        clock_requirement=ClockRequirement.SYNCHRONISED_REQUIRED,
        uncertainty_rule=UncertaintyRule(
            "exact_identity", _quantity(0.0, NCI_UNIT, "calibration/ue-level-serving-cell")
        ),
    )


class UELevelTargetFamily(ObjectiveFamilyModule):
    """Seats frozen by ``docs/architecture/SEAMS-GATE4.md``; bodies owned by OBJ2."""

    family = "UELevelTarget"
    lane = "OBJ2"

    def contract_bundle(
        self, *, scope: Mapping[str, Any], deployment_binding: DeploymentBinding
    ) -> ObjectiveContractBundle:
        """Target/Harm/Measurement set for a per-UE condition, over a measurement that is actually per-UE.

        Body owned by lane OBJ2 (``docs/architecture/SEAMS-GATE4.md``).

        ``scope`` names the two things a per-UE identity trial needs and this
        seat does not invent: ``scope["ueId"]`` is the UE, and
        ``scope["targetServingCell"]`` is the ``CId.ncI`` this trial holds it
        to.  The predicate is the MIN/MAX identity pair over one E2SM-KPM
        Style 4 counter, mandatory on both ends -- "no sample in the hold
        observed a cell other than the stated one" -- which is a *stronger*
        claim than a single end-of-episode readback and needs no per-UE rate
        measurement to state.
        """
        ue_id = str(scope["ueId"])
        target_nci = int(scope["targetServingCell"])
        home_nci = int(scope.get("homeServingCell", HOME_NCI))
        scope_selector = {"ueId": ue_id}
        baseline_config: Dict[str, Any] = {"servingCell": str(home_nci)}
        safe_state: Dict[str, Any] = dict(baseline_config)

        counter = CounterBinding(
            counter_id=COUNTER_ID,
            deployment_counter_name="UE.ServingCell",
            source=MeasurementSource.E2_KPM,
            scope_keys=("ueId",),
            unit=NCI_UNIT,
            native_cadence_ms=CADENCE_MS,
            deployment_binding_ref=deployment_binding.contract_id,
        )
        floor = _serving_cell_measurement(
            "measurement/ue-level-serving-cell-min",
            aggregation=Aggregation.MIN,
            ue_id=ue_id,
        )
        ceiling = _serving_cell_measurement(
            "measurement/ue-level-serving-cell-max",
            aggregation=Aggregation.MAX,
            ue_id=ue_id,
        )
        pinned = _quantity(float(target_nci), NCI_UNIT, "plan/ue-level-target-cell")
        predicates = (
            TargetPredicate(
                "ue-level-serving-cell-is-target-throughout",
                TypedConstraint(
                    "measurement/ue-level-serving-cell-min", ComparisonOperator.EQUAL, pinned
                ),
                description="no sample in the hold observed a lower cell identity",
            ),
            TargetPredicate(
                "ue-level-serving-cell-is-target-only",
                TypedConstraint(
                    "measurement/ue-level-serving-cell-max", ComparisonOperator.EQUAL, pinned
                ),
                description="no sample in the hold observed a higher cell identity",
            ),
        )
        option = TargetOption(
            **_identity("option/ue-level-serving-cell"),
            capability_ref="capability/ue-level-serving-cell",
            parameter_space={"servingCell": (str(target_nci),)},
        )
        target = TargetContract(
            **_identity("target/ue-level-target"),
            objective_family=self.family,
            scope_selector=scope_selector,
            predicates=predicates,
            options=(option,),
            hold_ms=HOLD_MS,
        )
        vector = TargetVector(
            **_identity("vector/ue-level-target"),
            ordered_target_refs=("target/ue-level-target",),
        )
        release = TargetReleasePolicy(**_identity("release/ue-level-target"))
        case_policy = CoordinationCasePolicy(
            **_identity("case-policy/ue-level-target"),
            deadline_ms=600_000,
            max_trials=2,
            max_proposals=4,
            target_release_policy_ref="release/ue-level-target",
            harm_contract_refs=("harm/ue-level-target",),
        )
        watchdog = WatchdogContract(
            **_identity("watchdog/ue-level-serving-cell"),
            watchdog_id="wd/ue-level-serving-cell",
            trigger=TypedConstraint(
                "measurement/ue-level-serving-cell-min", ComparisonOperator.EQUAL, pinned
            ),
            action=WatchdogAction.STOP_AND_ROLLBACK,
        )
        bound = CertifiedHarmBound(
            "bound/ue-level-target",
            _quantity(20.0, "ms", "calibration/ue-level-bound"),
            _quantity(2.0, "ms", "calibration/ue-level-uncertainty"),
            _quantity(22.0, "ms", "calibration/ue-level-conservative"),
            "measurement/ue-level-serving-cell-min#uncertainty",
            scope_selector,
            # `_enforced_timeout_ms` 는 한 harm 계약의 bound 중 **가장 짧은 것**을 고른다.
            # 그래서 여기 하나만 10초로 남아 있어도 다른 bound 를 30초로 올린 것이 전부
            # 무효가 된다 (2026-09-23: traffic_steering 과 joint 만 고쳤더니 정책은 계속
            # `actionDeadlineMs: 10000` 이었다).  형제 자리를 전부 같은 출처로 묶는다.
            enforced_timeout_ms_default(),
            ("calibration/ue-level-1",),
            "proof/ue-level-target-v1",
        )
        harm = HarmContract(
            **_identity("harm/ue-level-target"),
            harm_kind=HarmKind.TRIAL_INDUCED,
            scope_selector=scope_selector,
            reserve=_quantity(100.0, "ms", "plan/ue-level-reserve"),
            bounds=(bound,),
            watchdogs=(watchdog,),
            missing_interval_charge=_quantity(5.0, "ms", "plan/ue-level-missing-charge"),
        )
        actuator = ActuatorBinding(
            **_identity("actuator/ue-level-serving-cell"),
            capability_ref="capability/ue-level-serving-cell",
            path=ActuatorPath.OFFICIAL_ORAN_DYNAMIC,
            policy_type_id=POLICY_TYPE_ID,
            service_model={"serviceModel": "E2SM-RC", "style": "3", "action": "1"},
            readback_measurement_ref="measurement/ue-level-serving-cell-min",
            deployment_binding_ref=deployment_binding.contract_id,
        )
        capability = CapabilityManifest(
            **_identity("capability/ue-level-serving-cell"),
            capability_id="capability/ue-level-serving-cell",
            supported_objectives=(self.family,),
            constraints=(predicates[0].constraint,),
            actuator_refs=("actuator/ue-level-serving-cell",),
            measurement_refs=(
                "measurement/ue-level-serving-cell-min",
                "measurement/ue-level-serving-cell-max",
            ),
            interface_versions={"a1p": "2.0", "e2smRc": "1.03", "e2smKpm": "2.03"},
        )
        composition = CompositionManifest(
            **_identity("composition/ue-level-target"),
            composition_id="composition/ue-level-target",
            capability_refs=("capability/ue-level-serving-cell",),
        )
        return ObjectiveContractBundle(
            family=self.family,
            counters=(counter,),
            measurements=(floor, ceiling),
            target=target,
            vector=vector,
            release=release,
            case_policy=case_policy,
            watchdogs=(watchdog,),
            harm=harm,
            deployment=deployment_binding,
            actuators=(actuator,),
            capabilities=(capability,),
            composition=composition,
            baseline_config=baseline_config,
            safe_state=safe_state,
            scope=scope_selector,
            sample_scope={"ueId": ue_id},
        )

    def candidate_parameters(self) -> Mapping[str, Tuple[str, ...]]:
        """The finite parameter space of the per-UE action.

        Body owned by lane OBJ2 (``docs/architecture/SEAMS-GATE4.md``).

        The catalog this deployment can actually generate: the two cells
        Gate 3's OTA run named, ``HOME_NCI`` and ``TARGET_NCI``.  One
        ``contract_bundle`` call still names exactly one of them per trial
        (the identity predicate pins one value, the way the preserved
        ``PIN_TO_CELL`` contract does); this seat is the family-wide
        declaration of what that one value may be drawn from.
        """
        return {"servingCell": (str(HOME_NCI), str(TARGET_NCI))}

    def policy_lifecycle(self) -> PolicyLifecycle:
        """R1 and A1-P lifecycle for a UE-scoped submission.

        Body owned by lane OBJ2 (``docs/architecture/SEAMS-GATE4.md``).

        Names the same frozen policy type the preserved ``PIN_TO_CELL``
        contract and the ``TrafficSteeringPreference`` family use: this
        deployment has never registered one specific to per-UE identity, and
        the registry records this family as unsubmittable for exactly that
        reason (kind ``PIN_TO_CELL`` is the only one the type advertises).
        ``enforcement_evidence`` is deliberately not the A1-P create response
        or an E2SM-RC control acknowledgement (task section 7.6): the R1
        layer beneath this family reports enforcement through
        ``enforceStatus``/``policyState`` on the policy status object, and
        none of that -- nor the object existing -- is treated as enforced
        without the contracted readback confirming it.
        """
        return PolicyLifecycle(
            policy_type_id=POLICY_TYPE_ID,
            states=(
                "R1_REQUEST_SUBMITTED",
                "A1_POLICY_CREATED",
                "A1_POLICY_ENFORCE_STATUS_REPORTED",
                "EFFECT_READBACK_CONFIRMED",
                "R1_STATUS_REPORTED",
            ),
            idempotency_basis=(
                "the A1-P policy object id is derived deterministically from "
                "(ueId, targetPrimaryCellId); a retransmitted PUT with the same "
                "id and body updates nothing and creates no second steering "
                "action"
            ),
            enforcement_evidence=(
                "an E2SM-KPM Style 4 UE.ServingCell readback attributing the UE "
                "to the target cell for the full hold, not the A1-P create "
                "response or the E2SM-RC control acknowledgement"
            ),
            notes=(
                "No policy type in this deployment advertises a per-UE identity "
                "objective kind; this lifecycle actuates through the same "
                "frozen AIC_UECellSteering_1.0.0 type PIN_TO_CELL and "
                "TrafficSteeringPreference use, which is why "
                "objective/UELevelTarget is recorded unsubmittable even once "
                "hardware-free verified."
            ),
        )

    def kpi_declaration(self) -> Tuple[KpiDeclaration, ...]:
        """Decision and assurance KPIs at UE scope.  A cell-scope measurement may not be declared here as if it were per-UE.

        Body owned by lane OBJ2 (``docs/architecture/SEAMS-GATE4.md``).

        Both entries are the assurance population: the identity pair is what
        the mandatory predicates read, and this deployment declares no
        separate per-UE quantity to steer candidate selection by, so there is
        no decision-KPI entry to add without inventing one.
        """
        return (
            KpiDeclaration(
                measurement_ref="measurement/ue-level-serving-cell-min",
                use=KpiUse.ASSURANCE,
                scope_level="UE",
                unit=NCI_UNIT,
                cadence_ms=CADENCE_MS,
                freshness_bound_ms=FRESHNESS_MS,
                source_interface=(
                    "E2SM-KPM 2.03 Style 4 (E2SM_KPM_STYLE4_UEID_NODE_ATTRIBUTION), "
                    "ranFunctionId 2"
                ),
                provenance_note="lower bound of the identity pair; MIN over the hold",
            ),
            KpiDeclaration(
                measurement_ref="measurement/ue-level-serving-cell-max",
                use=KpiUse.ASSURANCE,
                scope_level="UE",
                unit=NCI_UNIT,
                cadence_ms=CADENCE_MS,
                freshness_bound_ms=FRESHNESS_MS,
                source_interface=(
                    "E2SM-KPM 2.03 Style 4 (E2SM_KPM_STYLE4_UEID_NODE_ATTRIBUTION), "
                    "ranFunctionId 2"
                ),
                provenance_note="upper bound of the identity pair; MAX over the hold",
            ),
        )

    def terminal_oracle(self, evaluation: Mapping[str, Any]) -> TrialOutcome:
        """Name the terminal outcome for this family from the Kernel's axes.

        Body owned by lane OBJ2 (``docs/architecture/SEAMS-GATE4.md``).

        Not a second evaluator: ``evaluation`` already carries the Kernel's
        own execution validity, measurement sufficiency and per-predicate
        verdicts (design section 7 step 8's success rule, and the same
        fields :meth:`assurance.kernel.kernel.AssuranceKernel.decide_trial`
        reads).  This identity objective adds no family-specific relaxation
        of that rule -- an identity predicate is judged exactly like any
        other mandatory predicate -- so the oracle restates the Kernel's own
        classification rather than deciding a second one.  Safety stops
        (stale telemetry, partial apply, lease expiry, operator abort) are
        recorded on the trial outside this mapping and are not this seat's
        to re-derive.
        """
        execution_validity = evaluation.get("executionValidity")
        if execution_validity == ExecutionValidity.EXEC_ERROR.value:
            return TrialOutcome.EXEC_ERROR
        if execution_validity == ExecutionValidity.INVALID.value:
            return TrialOutcome.INVALID
        predicate_verdicts = dict(evaluation.get("predicateVerdicts") or {})
        mandatory_ids = tuple(evaluation.get("mandatoryPredicateIds", ()))
        measurement_sufficiency = evaluation.get("measurementSufficiency")
        mandatory_verdicts = tuple(predicate_verdicts.get(pid) for pid in mandatory_ids)
        success = (
            execution_validity == ExecutionValidity.VALID.value
            and measurement_sufficiency == MeasurementSufficiency.SUFFICIENT.value
            and bool(mandatory_verdicts)
            and all(verdict == PredicateVerdict.PASS.value for verdict in mandatory_verdicts)
            and bool(evaluation.get("holdComplete"))
            and bool(evaluation.get("sameCandidate"))
            and bool(evaluation.get("validityRegionStable"))
        )
        if success:
            return TrialOutcome.SUCCESS
        if measurement_sufficiency != MeasurementSufficiency.SUFFICIENT.value:
            return TrialOutcome.INDETERMINATE
        return TrialOutcome.FAIL

    def hardware_free_expectations(self) -> Mapping[ScenarioName, ExpectedOutcome]:
        """What each verdict-bearing scenario of the shared matrix must end as.

        Body owned by lane OBJ2 (``docs/architecture/SEAMS-GATE4.md``).

        ``partial-effect`` is real for this deployment even though the
        control surface is a single axis, once the axis is read at the
        right layer.  A single RC control axis cannot land *halfway* the way
        a multi-axis plan can, but this family's own lifecycle
        (:meth:`policy_lifecycle`) already separates the A1-P policy object
        from the RC control action it is supposed to cause -- exactly the
        gap task section 7.6 names when it refuses to accept "A1 create" as
        success.  The case supplied to the matrix (``faults_for``) refuses
        the RC axis at commit, after prepare and ready have already
        validated the plan: the policy object exists, the control it should
        have caused at the RAN never lands, and the gateway confirms the
        baseline is still live by readback rather than guessing.  Empirically
        distinct from ``fault`` (which refuses at prepare, before any commit
        or readback): this deployment settles it ``SETTLED_NON_SUCCESS`` /
        ``SAFETY_STOPPED`` with no stop reason recorded, because the
        post-commit recovery sweep resolves it cleanly rather than treating
        it as an execution error.
        """
        from assurance.core.states import TrialState

        return {
            ScenarioName.POSITIVE: ExpectedOutcome(
                terminal_state=TrialState.SETTLED_SUCCESS,
                outcome=TrialOutcome.SUCCESS,
                evidence_status="CLOSED_PASS",
                configuration=ExpectedConfiguration.APPLIED,
                rationale=(
                    "every sample of the hold attributed the UE to the target "
                    "cell, so both ends of the identity pair pass and the "
                    "change is finalized live"
                ),
            ),
            ScenarioName.NEGATIVE: ExpectedOutcome(
                terminal_state=TrialState.SETTLED_NON_SUCCESS,
                outcome=TrialOutcome.FAIL,
                stop_reason=StopReason.SEMANTIC_NON_SUCCESS,
                evidence_status="CLOSED_FAIL",
                rationale=(
                    "a cleanly measured serving cell other than the target one "
                    "is a semantic failure: reversed to the baseline cell, and "
                    "the cell closes as a fail rather than staying open"
                ),
            ),
            ScenarioName.STALE: ExpectedOutcome(
                terminal_state=TrialState.SETTLED_NON_SUCCESS,
                outcome=TrialOutcome.SAFETY_STOPPED,
                stop_reason=StopReason.TELEMETRY_STALE,
                evidence_status="OPEN",
                rationale=(
                    "stale KPM attribution is a safety stop, not a verdict on "
                    "the identity predicate; the obligation stays open because "
                    "nothing was decided"
                ),
            ),
            ScenarioName.MISSING: ExpectedOutcome(
                terminal_state=TrialState.SETTLED_NON_SUCCESS,
                outcome=TrialOutcome.INDETERMINATE,
                stop_reason=StopReason.SEMANTIC_NON_SUCCESS,
                rationale=(
                    "no KPM attribution sample at all is insufficient coverage "
                    "for an identity predicate that needs every sample in the "
                    "hold, which decides nothing and fills no closure quota"
                ),
            ),
            ScenarioName.PARTIAL_EFFECT: ExpectedOutcome(
                terminal_state=TrialState.SETTLED_NON_SUCCESS,
                outcome=TrialOutcome.SAFETY_STOPPED,
                rationale=(
                    "the A1-P policy object is created but the RC control "
                    "action it is supposed to cause is refused at commit, "
                    "after prepare and ready already validated the plan; the "
                    "gateway confirms the baseline is still live by readback "
                    "rather than guessing, and the post-commit recovery sweep "
                    "settles it cleanly with no rollback needed"
                ),
            ),
            ScenarioName.FAULT: ExpectedOutcome(
                terminal_state=TrialState.SETTLED_NON_SUCCESS,
                outcome=TrialOutcome.EXEC_ERROR,
                rationale=(
                    "a PREPARE the RC control path refuses aborts before the "
                    "commit line, so the UE's serving cell was never touched"
                ),
            ),
        }
