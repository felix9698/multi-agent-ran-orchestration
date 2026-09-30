"""KCON contract admission, canonical addressing, and epoch freeze tests."""

from __future__ import annotations

from dataclasses import replace
import unittest

from assurance.contracts.catalog import DomainMembership
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
    ContractAdmissionError,
    CounterBinding,
    DeploymentBinding,
    EvidenceCell,
    EvidenceContribution,
    EvidenceLedgerRecord,
    Estimator,
    GapPolicy,
    HarmContract,
    HarmKind,
    HarmLedgerRecord,
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
    CompatibilityCheck,
    CompatibilityRecord,
    MovementKind,
    ReserveMovement,
    catalog_hash,
    contract_content_hash,
    freeze_epoch,
    generate_catalog,
    validate_contract,
    validate_family_set,
)
from assurance.core.confirmation import ConfirmationAction, ConfirmationRecord
from assurance.core.axes import EvidenceCellStatus, ExecutionValidity, MeasurementSufficiency, PredicateVerdict
from assurance.core.provenance import DocumentStatus, Provenance, TypedQuantity


IDENTITY = {
    "contract_id": "contract/test",
    "version": "1.0.0",
    "schema_version": "1.0.0",
    "document_status": "NORMATIVE",
    "standard_mapping": {"a1": "1.0"},
}


def identity(contract_id: str, **overrides: object) -> dict[str, object]:
    result: dict[str, object] = {**IDENTITY, "contract_id": contract_id}
    result.update(overrides)
    return result


def quantity(value: float = 1.0, unit: str = "ms") -> TypedQuantity:
    return TypedQuantity(value, unit, Provenance.EXPERIMENT_CONFIG, "plan/test")


def contract_set() -> dict[str, object]:
    uncertainty = UncertaintyRule("bounded_absolute", quantity(1.0, "ms"))
    counter = CounterBinding(
        "counter/latency", "latency_ms", MeasurementSource.E2_KPM,
        ("cell",), "ms", 1000, "deployment/e2",
    )
    measurement = MeasurementContract(
        **identity("measurement/latency"), counter_id="counter/latency",
        scope_selector={"cell": "c1"}, membership_snapshot=("ue1",),
        cadence_ms=1000, window_width_ms=5000, window_stride_ms=5000,
        overlap=OverlapPolicy.DISJOINT, aggregation=Aggregation.P95,
        estimator=Estimator.EMPIRICAL_QUANTILE, minimum_entity_count=1,
        hold_ms=5000, gap_policy=GapPolicy.CONSERVATIVE_CHARGE,
        missing_interval_charge=quantity(25.0, "ms"), freshness_bound_ms=1000,
        clock_requirement=ClockRequirement.SYNCHRONISED_REQUIRED,
        uncertainty_rule=uncertainty,
    )
    constraint = TypedConstraint(
        "measurement/latency", ComparisonOperator.LESS_OR_EQUAL, quantity(20.0, "ms")
    )
    option = TargetOption(
        **identity("option/steer"), capability_ref="capability/steer",
        parameter_space={"targetCell": ("c2", "c3")},
    )
    target = TargetContract(
        **identity("target/steer"), objective_family="TrafficSteeringPreference",
        scope_selector={"cell": "c1"}, predicates=(TargetPredicate("p95", constraint),),
        options=(option,), hold_ms=5000,
    )
    target_vector = TargetVector(
        **identity("vector/main"), ordered_target_refs=("target/steer",)
    )
    release = TargetReleasePolicy(**identity("release/main"))
    case_policy = CoordinationCasePolicy(
        **identity("case/main"), deadline_ms=60000, max_trials=4, max_proposals=8,
        target_release_policy_ref="release/main", harm_contract_refs=("harm/trial",),
    )
    watchdog = WatchdogContract(
        **identity("watchdog/latency"), watchdog_id="wd/latency", trigger=constraint,
        action=WatchdogAction.STOP_AND_ROLLBACK,
    )
    bound = CertifiedHarmBound(
        "bound/latency", quantity(20.0, "ms"), quantity(2.0, "ms"),
        quantity(22.0, "ms"), "measurement/latency#uncertainty", {"cell": "c1"},
        10000, ("calibration/1",), "proof/latency-v1",
    )
    harm = HarmContract(
        **identity("harm/trial"), harm_kind=HarmKind.TRIAL_INDUCED,
        scope_selector={"cell": "c1"}, reserve=quantity(100.0, "ms"),
        bounds=(bound,), watchdogs=(watchdog,), missing_interval_charge=quantity(25.0, "ms"),
    )
    deployment = DeploymentBinding(
        **identity("deployment/e2"), endpoint_id="e2", base_url="https://e2.example",
        transport_security=TransportSecurity.MTLS, secret_refs={"client": "env:E2_SECRET"},
        trust_anchor_ref="file:/etc/ssl/e2-ca.pem",
    )
    actuator = ActuatorBinding(
        **identity("actuator/steer"), capability_ref="capability/steer",
        path=ActuatorPath.OFFICIAL_ORAN_DYNAMIC, policy_type_id="100",
        service_model={"serviceModel": "E2SM-RC"},
        readback_measurement_ref="measurement/latency",
        deployment_binding_ref="deployment/e2",
    )
    capability = CapabilityManifest(
        **identity("capability/steer"), capability_id="capability/steer",
        supported_objectives=("TrafficSteeringPreference",), constraints=(constraint,),
        actuator_refs=("actuator/steer",), measurement_refs=("measurement/latency",),
        interface_versions={"e2smRc": "1.0"},
    )
    composition = CompositionManifest(
        **identity("composition/main"), composition_id="composition/main",
        capability_refs=("capability/steer",),
    )
    return locals()


class ContractAdmissionTests(unittest.TestCase):
    def test_round_trip_contract_set_is_admitted(self) -> None:
        data = contract_set()
        contracts = [
            data[name] for name in (
                "counter", "measurement", "option", "target", "target_vector", "release", "case_policy",
                "watchdog", "harm", "deployment", "actuator", "capability", "composition",
            )
        ]
        for contract in contracts:
            validate_contract(contract)
        validate_family_set(contracts)

    def test_illustrative_success_threshold_is_refused(self) -> None:
        data = contract_set()
        illustrative = TypedQuantity(
            20.0, "ms", Provenance.EXPERIMENT_CONFIG, "slide/1",
            document_status=DocumentStatus.ILLUSTRATIVE,
        )
        predicate = TargetPredicate(
            "p95", TypedConstraint("measurement/latency", ComparisonOperator.LESS_OR_EQUAL, illustrative)
        )
        target = replace(data["target"], predicates=(predicate,))
        with self.assertRaises(ContractAdmissionError):
            validate_contract(target)

    def test_measurement_missing_required_field_is_refused_at_construction(self) -> None:
        data = contract_set()
        kwargs = dict(data["measurement"].__dict__)
        kwargs.pop("uncertainty_rule")
        with self.assertRaises(TypeError):
            MeasurementContract(**kwargs)

    def test_sample_max_only_harm_bound_is_refused(self) -> None:
        data = contract_set()
        unsafe = replace(
            data["harm"],
            bounds=(replace(data["bound"], enforced_timeout_ms=0, calibration_records=(), proof_ref=None),),
        )
        with self.assertRaises(ContractAdmissionError):
            validate_contract(unsafe)

    def test_deployment_rejects_secret_value_or_url_userinfo(self) -> None:
        data = contract_set()
        inline = replace(data["deployment"], secret_refs={"client": "actual-password"})
        with self.assertRaises(ContractAdmissionError):
            validate_contract(inline)
        with self.assertRaises(ContractAdmissionError):
            validate_contract(replace(data["deployment"], base_url="https://user:pass@e2.example"))


class AddressingCatalogAndEpochTests(unittest.TestCase):
    def test_digest_is_deterministic_for_defensively_copied_contract(self) -> None:
        data = contract_set()
        target = data["target"]
        first = contract_content_hash(target)
        source = {"cell": "c1"}
        copied = replace(target, scope_selector=source)
        source["cell"] = "changed-after-construction"
        self.assertEqual(first, contract_content_hash(copied))

    def test_catalog_generation_is_deterministic_and_finite(self) -> None:
        data = contract_set()
        args = dict(
            targets=(data["target"],), capabilities=(data["capability"],),
            composition=data["composition"], generator_version="catalog-v1", epoch_ref="epoch/1",
            identity=identity("catalog/main"),
        )
        first = generate_catalog(**args)
        second = generate_catalog(**args)
        self.assertEqual(first, second)
        self.assertEqual(first.cardinality, 2)
        self.assertTrue(first.membership_matches_cardinality())
        self.assertEqual(first.catalog_hash, catalog_hash(
            generator_version=first.generator_version, cardinality=first.cardinality,
            candidates=first.candidates,
        ))

    def test_a_value_listed_twice_on_an_axis_does_not_duplicate_a_candidate(self) -> None:
        """2026-09-18: 17판 중 2판이 이것 때문에 에피소드를 시작조차 못 했다.

        축의 **현재값**이 제 사다리의 한 칸과 같으면 그 값이 두 번 실리고,
        `product` 가 같은 조합을 두 번 낸다.  두 후보의 parameters 가 같으면
        semantic_hash 도 같고, epoch 검증기가
        `epoch candidate membership is not unique` 로 통째로 거절한다 --
        판은 exit.json 과 트레이스백만 남기고 죽는다(에피소드 기록 없음).

        pfWeight 는 1.0/4.0 이고 기준이 1.0, 감쇠 사다리는 제 기준값에서
        시작한다(12345678 은 0.0, 87654321 은 10.0).  그래서 **판이 뜨는지가
        앞 판이 라디오에 남긴 값에 달려** 있었다.
        """
        from dataclasses import replace
        data = contract_set()
        dup = replace(data["target"].options[0],
                      parameter_space={"targetCell": ("c2", "c3", "c2")})
        target = replace(data["target"], options=(dup,))
        catalog = generate_catalog(
            targets=(target,), capabilities=(data["capability"],),
            composition=data["composition"], generator_version="catalog-v1",
            epoch_ref="epoch/1", identity=identity("catalog/dup"))
        hashes = [c.semantic_hash for c in catalog.candidates]
        self.assertEqual(len(hashes), len(set(hashes)), "같은 후보가 두 번 생겼다")
        self.assertEqual(catalog.cardinality, 2)          # c2 와 c3, 둘뿐
        self.assertEqual(sorted(c.parameters["targetCell"] for c in catalog.candidates),
                         ["c2", "c3"])

    def test_epoch_freeze_is_stable_and_catalog_membership_is_immutable(self) -> None:
        data = contract_set()
        catalog = generate_catalog(
            targets=(data["target"],), capabilities=(data["capability"],),
            composition=data["composition"], generator_version="catalog-v1", epoch_ref="epoch/1",
            identity=identity("catalog/main"),
        )
        vector_hash = contract_content_hash(data["target_vector"])
        confirmation = ConfirmationRecord(
            "TargetVector", vector_hash, "confirm/1", "2026-08-21T09:00:00.000000Z",
            ConfirmationAction.CONFIRM_AND_START,
        )
        args = dict(
            identity=identity("epoch/1"), epoch_id="epoch/1", frozen_at="2026-08-21T09:01:00.000000Z",
            targets=(data["target"],), harms=(data["harm"],), measurements=(data["measurement"],),
            capabilities=(data["capability"],), composition=data["composition"],
            target_vector=data["target_vector"], case_policy=data["case_policy"],
            deployment_bindings=(data["deployment"],), counter_bindings=(data["counter"],),
            actuator_bindings=(data["actuator"],), catalog=catalog, evaluator_version="eval-v1",
            reducer_version="reducer-v1", confirmation=confirmation,
        )
        first = freeze_epoch(**args)
        second = freeze_epoch(**args)
        self.assertEqual(first, second)
        with self.assertRaises(AttributeError):
            first.candidate_semantic_hashes += ("0" * 64,)
        # 2026-09-19: the membership is a frozen domain.  A decoded point is a
        # fresh object, so editing it cannot reach the catalog ...
        catalog.candidates[0].parameters["targetCell"] = "mutated"
        self.assertEqual(first, freeze_epoch(**args))
        self.assertNotEqual("mutated", catalog.candidates[0].parameters["targetCell"])
        # ... and a changed domain is a changed hash, refused at freeze.
        blocks = catalog.candidates.declaration()
        blocks[0]["values"][0] = list(blocks[0]["values"][0]) + ["tampered"]
        blocks[0]["size"] = 1
        for values in blocks[0]["values"]:
            blocks[0]["size"] *= len(values)
        tampered = replace(catalog, candidates=DomainMembership(blocks),
                           cardinality=blocks[0]["size"])
        with self.assertRaises(ContractAdmissionError):
            freeze_epoch(**{**args, "catalog": tampered})
        with self.assertRaises(ContractAdmissionError):
            freeze_epoch(**{**args, "catalog": replace(catalog, cardinality=1)})


class LedgerContractTests(unittest.TestCase):
    def contribution(self, *, trace: str, group: str | None = None, witness: bool = False) -> EvidenceContribution:
        return EvidenceContribution(
            contribution_id=f"contribution/{trace}", trial_ref=f"trial/{trace}",
            candidate_semantic_hash="a" * 64, execution_validity=ExecutionValidity.VALID,
            measurement_sufficiency=MeasurementSufficiency.SUFFICIENT,
            predicate_verdict=PredicateVerdict.PASS, trace_refs=(trace,),
            dependency_group=group, is_post_closure_witness=witness,
        )

    def test_evidence_rejects_duplicate_trace_or_dependency_group(self) -> None:
        duplicate_trace = EvidenceCell(
            "cell/1", "target/steer", "a" * 64, EvidenceCellStatus.PARTIAL, 2,
            (self.contribution(trace="trace/1", group="group/1"), self.contribution(trace="trace/1", group="group/2")),
        )
        with self.assertRaises(ContractAdmissionError):
            validate_contract(duplicate_trace)
        duplicate_group = replace(
            duplicate_trace,
            contributions=(self.contribution(trace="trace/1", group="group/1"), self.contribution(trace="trace/2", group="group/1")),
        )
        with self.assertRaises(ContractAdmissionError):
            validate_contract(duplicate_group)

    def test_dormant_and_post_closure_evidence_require_explicit_append_state(self) -> None:
        dormant = EvidenceCell(
            "cell/1", "target/steer", "a" * 64, EvidenceCellStatus.DORMANT_SEALED, 1,
            sealed_until_vector_ref="vector/main",
        )
        validate_contract(dormant)
        witness = self.contribution(trace="trace/witness", witness=True)
        record = EvidenceLedgerRecord(
            "ledger/1", "event/1", "epoch/1", "case/1",
            EvidenceCell("cell/1", "target/steer", "a" * 64, EvidenceCellStatus.POST_CLOSURE_WITNESS, 1),
            added_contribution=witness, previous_status=EvidenceCellStatus.CLOSED_FAIL,
        )
        validate_contract(record)
        with self.assertRaises(ContractAdmissionError):
            validate_contract(replace(record, previous_status=EvidenceCellStatus.PARTIAL))

    def test_cross_epoch_reuse_requires_all_compatibility_checks(self) -> None:
        results = {check.value: True for check in CompatibilityCheck}
        record = CompatibilityRecord("compat/1", "epoch/0", "epoch/1", "a" * 64, results, True)
        validate_contract(record)
        with self.assertRaises(ContractAdmissionError):
            validate_contract(replace(record, results={CompatibilityCheck.SEMANTICS.value: True}))
        with self.assertRaises(ContractAdmissionError):
            validate_contract(replace(record, results={**results, CompatibilityCheck.DRIFT.value: False}))

    def test_target_debt_and_trial_harm_are_separate_typed_ledger_buckets(self) -> None:
        movement = ReserveMovement("move/1", MovementKind.CHARGE, quantity(1.0, "ms"), "measured")
        trial = HarmLedgerRecord("harm/1", "event/1", "epoch/1", "case/1", "harm/trial", HarmKind.TRIAL_INDUCED, movement)
        debt = replace(trial, record_id="harm/2", harm_kind=HarmKind.TARGET_DEBT)
        validate_contract(trial)
        validate_contract(debt)
        self.assertIsNot(trial.harm_kind, debt.harm_kind)
