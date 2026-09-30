"""The objective registry says what is true, and refuses to say more.

Gate 4's fourth acceptance criterion is that each objective's actual capability
and evidence level are honestly shown (task section 13), and its third is that a
mock or replay result is never displayed as OTA success.  Both are properties of
the registry, so both are asserted here rather than described.

Three kinds of claim in this file:

* the registry as it stands is admissible, complete and consistent with the
  deployment it describes -- checked against ``oran/integration/objectives.py``,
  which is the module that actually refuses a submission;
* the honesty rules bite: for each rule in
  :func:`~assurance.objectives.registry.validate_record`, a record that breaks
  it is refused;
* the preserved ``PIN_TO_CELL`` regression contract is untouched, and the
  relationship to the new Traffic Steering family is a versioned mapping rather
  than a rename.
"""

from __future__ import annotations

import re
import unittest
from dataclasses import replace
from pathlib import Path

from assurance.contracts.validation import contract_content_hash
from assurance.objectives.registry import (
    BLOCKING_PREMISE_KINDS,
    OBJECTIVE_FAMILIES,
    OBJECTIVE_REGISTRY,
    PIN_REGRESSION_FAMILY,
    PROJECT_IDENTIFIER_NOTICE,
    DeploymentCapability,
    EvidenceLevel,
    Premise,
    PremiseKind,
    RegistryError,
    SupportState,
    record_for,
    registry_view,
    validate_record,
    validate_registry,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

#: ``A2`` as a standalone token, in any case.  Task section 7.9: this system
#: has no ``A2`` interface and no document of it may invent one.
A2_TOKEN = re.compile(r"(?<![0-9A-Za-z])a2(?![0-9A-Za-z])", re.IGNORECASE)

#: The exact content hash of the frozen ``PIN_TO_CELL`` target contract at the
#: baseline this gate started from.  Pinned, not derived: task section 8 keeps
#: that contract as an exact regression path, so a change to it must fail a
#: test rather than travel silently with a Gate 4 edit.
PIN_TARGET_CONTENT_HASH = (
    "52bcf36f8a952752b1d17100b8990daa1a3856615e500be168195e8e3d447c67"
)


class RegistryAdmissionTests(unittest.TestCase):
    def test_the_registry_as_it_stands_is_admissible(self) -> None:
        validate_registry(OBJECTIVE_REGISTRY)

    def test_the_seven_families_and_the_regression_contract_are_all_present(
        self,
    ) -> None:
        families = [record.family for record in OBJECTIVE_REGISTRY]
        self.assertEqual(
            families[:7],
            [
                "TrafficSteeringPreference", "QoSTarget", "QoSandTSP",
                "UELevelTarget", "QoETarget", "QoEandTSP", "SliceSLATarget",
            ],
        )
        self.assertEqual(set(families), set(OBJECTIVE_FAMILIES) | {PIN_REGRESSION_FAMILY})
        self.assertEqual(len(families), 8)

    def test_support_states_are_exactly_the_five_the_design_lists(self) -> None:
        self.assertEqual(
            {state.value for state in SupportState},
            {
                "IMPLEMENTATION_IN_PROGRESS",
                "HARDWARE_FREE_VERIFIED",
                "OTA_VERIFICATION_PENDING",
                "OTA_LIVE_VERIFIED",
                "UNSUPPORTED_BY_CURRENT_DEPLOYMENT",
            },
        )

    def test_every_record_is_a_project_contract_identifier(self) -> None:
        for record in OBJECTIVE_REGISTRY:
            with self.subTest(family=record.family):
                self.assertEqual(
                    record.project_contract_id, f"objective/{record.family}"
                )
                self.assertEqual(
                    record.as_dict()["identifierKind"], "PROJECT_CONTRACT_IDENTIFIER"
                )
                self.assertIn("project contract identifier", record.as_dict()["identifierNotice"])

    def test_every_record_maps_to_a_published_version(self) -> None:
        """Task section 7.8: mapped explicitly, or not admitted."""
        for record in OBJECTIVE_REGISTRY:
            with self.subTest(family=record.family):
                mapping = record.standard_mapping
                self.assertTrue(mapping.policy_interface)
                self.assertTrue(
                    mapping.control_service_models
                    or mapping.measurement_service_models
                    or mapping.o1_measurements
                )
                for model in (
                    mapping.control_service_models + mapping.measurement_service_models
                ):
                    self.assertTrue(model.version, model.service_model)
                    self.assertTrue(model.basis, model.service_model)
                for measurement in mapping.o1_measurements:
                    self.assertTrue(measurement.definition)
                    self.assertTrue(measurement.clause)

    def test_no_record_names_an_interface_this_system_does_not_have(self) -> None:
        for path, text in _strings(registry_view()):
            with self.subTest(path=path):
                self.assertIsNone(A2_TOKEN.search(text), f"{path}: {text}")

    def test_the_objectives_package_source_names_no_such_interface_either(self) -> None:
        """The prohibition is stated in exactly one place: the rule that enforces it.

        ``registry.py`` has to write the token down in order to refuse it, so
        the scan permits it there and nowhere else -- and only on lines that are
        part of the rule.  Anywhere else in the package, and in any line of
        ``registry.py`` that is not the rule, the token would be documentation
        of an interface this system does not have.
        """
        package = REPOSITORY_ROOT / "assurance" / "objectives"
        rule_marker = ("_A2_TOKEN", "section 7.9", "non-existent", "DATA2")
        for module in sorted(package.rglob("*.py")):
            for number, line in enumerate(
                module.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not A2_TOKEN.search(line):
                    continue
                where = f"{module.relative_to(REPOSITORY_ROOT)}:{number}"
                self.assertEqual(
                    module.name, "registry.py", f"{where}: {line.strip()}"
                )
                self.assertTrue(
                    any(marker in line for marker in rule_marker),
                    f"{where} names the token outside the rule: {line.strip()}",
                )

    def test_the_view_carries_the_identifier_notice(self) -> None:
        view = registry_view()
        self.assertEqual(view["identifierNotice"], PROJECT_IDENTIFIER_NOTICE)
        self.assertEqual(len(view["records"]), len(OBJECTIVE_REGISTRY))
        self.assertEqual(view["regressionContract"], PIN_REGRESSION_FAMILY)


class DeploymentAgreementTests(unittest.TestCase):
    """The registry's capability claims agree with the module that enforces them.

    ``assurance/**`` may not import ``oran.integration`` (the package boundary
    in ``tests/assurance/test_seams.py``), so the registry carries the capability
    facts as literals.  This is where the two are compared: a registry that
    drifted from the deployment would otherwise be honest-looking and wrong.
    """

    def test_the_deployment_executes_exactly_one_objective_kind(self) -> None:
        from oran.integration.objectives import EXECUTABLE_OBJECTIVES

        self.assertEqual(EXECUTABLE_OBJECTIVES, ("PIN_TO_CELL",))

    def test_only_the_wire_mapped_families_are_submittable(self) -> None:
        """Submittable is exactly "has a wire kind", and nothing else.

        Gate 4 recorded one submittable family because only the PIN_TO_CELL
        regression contract had an established mapping onto an advertised
        objective kind.  The dispatcher ruling of 2026-08-25 established that
        mapping for four more (``ASSURANCE_FAMILY_TO_WIRE_KIND`` v1.2.0), so the
        set moved -- but the *rule* did not: a family is submittable when, and
        only when, the versioned table gives it a kind the deployment
        advertises.  Asserting the rule rather than the literal list is what
        keeps the next entry honest.
        """
        from assurance.objectives.registry import ASSURANCE_FAMILY_TO_WIRE_KIND

        submittable = {
            record.family
            for record in OBJECTIVE_REGISTRY
            if record.deployment_capability.submittable
        }
        self.assertEqual(submittable, set(ASSURANCE_FAMILY_TO_WIRE_KIND))
        self.assertIn(PIN_REGRESSION_FAMILY, submittable)

    def test_every_wire_mapped_kind_is_one_the_deployment_advertises(self) -> None:
        """Section 7.7: the right-hand column must be a real advertised kind."""
        from assurance.objectives.registry import ASSURANCE_FAMILY_TO_WIRE_KIND
        from oran.integration.objectives import SCHEMA_OBJECTIVES

        for family, kind in ASSURANCE_FAMILY_TO_WIRE_KIND.items():
            with self.subTest(family=family):
                self.assertIn(kind, set(SCHEMA_OBJECTIVES))
                self.assertNotIn(family, set(SCHEMA_OBJECTIVES))

    def test_no_registry_family_is_an_objective_kind_the_deployment_refuses(
        self,
    ) -> None:
        """A family name must not be smuggled in as a submittable objective kind."""
        from oran.integration.objectives import SCHEMA_OBJECTIVES

        self.assertEqual(set(SCHEMA_OBJECTIVES), {"BALANCE_PRB_LOAD", "PIN_TO_CELL"})
        self.assertEqual(set(OBJECTIVE_FAMILIES) & set(SCHEMA_OBJECTIVES), set())

    def test_quality_families_use_the_versioned_steering_wire_mapping(self) -> None:
        from assurance.objectives.registry import (
            ASSURANCE_FAMILY_TO_WIRE_KIND,
            ASSURANCE_FAMILY_WIRE_KIND_MAPPING_VERSION,
        )

        self.assertEqual(ASSURANCE_FAMILY_WIRE_KIND_MAPPING_VERSION, "1.3.0")
        for family in ("QoSTarget", "QoSandTSP", "QoETarget", "QoEandTSP"):
            with self.subTest(family=family):
                record = record_for(family)
                control = record.standard_mapping.control_service_models
                self.assertEqual(ASSURANCE_FAMILY_TO_WIRE_KIND[family], "PIN_TO_CELL")
                self.assertEqual([model.style_id for model in control], [3])
                self.assertEqual([model.action_id for model in control], [1])
                self.assertEqual(
                    record.standard_mapping.policy_type_id,
                    "AIC_UECellSteering_1.0.0",
                )
                self.assertTrue(record.deployment_capability.submittable)
                self.assertTrue(record.deployment_capability.a1_policy_type_present)
                self.assertFalse(record.deployment_capability.blocking_reasons)
                if family.startswith("QoS"):
                    self.assertFalse(record.unmet_premises())
                    self.assertIn("QoS is achieved through cell selection", record.standard_mapping.notes)
                else:
                    self.assertTrue(all(p.live_only for p in record.unmet_premises()))

    def test_o1_and_kpm_format3_qos_measurement_facts_remain_distinct(self) -> None:
        record = record_for("QoSTarget")
        by_name = {
            measurement.name: measurement
            for measurement in record.standard_mapping.o1_measurements
        }
        self.assertTrue(by_name["RRU.PrbDl"].delivered_by_deployment)
        self.assertFalse(by_name["DRB.UEThpDl"].delivered_by_deployment)
        kpm_parameters = {
            parameter
            for model in record.standard_mapping.measurement_service_models
            for parameter in model.parameters
        }
        self.assertIn("DRB.UEThpDl", kpm_parameters)
        self.assertIn("UE.ServingCell", kpm_parameters)
        source_premise = next(
            premise
            for premise in record.premises
            if premise.kind is PremiseKind.MEASUREMENT_SOURCE
        )
        # The premise moved from "demonstrated historically, delivery is a
        # separate gate" to "delivered and judged in one hold", because that
        # is what the 2026-08-28 OTA run did -- the re-run under the corrected
        # lease and clock-health rules, which is the one the record cites.
        # The two counters stay distinct facts either way, which is what this
        # test is about.
        self.assertIn(
            "GATE5-OTA-QoSTarget-20260828T140345Z-run.json", source_premise.basis
        )
        self.assertIn("RRU.PrbDl", source_premise.basis)
        self.assertIn("DRB.UEThpDl", source_premise.basis)
        self.assertIn("without aliasing their scopes", source_premise.statement)

    def test_families_whose_premise_is_missing_say_which_premise(self) -> None:
        """Every family this deployment cannot support today names why.

        ``UELevelTarget`` is deliberately not in this list even though it
        was at the seam's baseline (section 5 of
        ``docs/architecture/SEAMS-GATE4.md`` states that baseline is a fact
        "at this seam's point in time", not a permanent one): OBJ2 settled
        its judgable per-UE quantity as an identity condition this
        deployment's own E2SM-KPM attribution actually delivers, so its
        premises are honestly all met -- see
        ``tests/assurance/test_obj2_ue_level_target.py`` for the matrix that
        earned it.
        """
        for family in ("QoETarget", "QoEandTSP", "SliceSLATarget"):
            with self.subTest(family=family):
                record = record_for(family)
                unmet = [
                    premise
                    for premise in record.unmet_premises()
                    if premise.kind in BLOCKING_PREMISE_KINDS
                ]
                self.assertTrue(unmet, f"{family} claims every premise is met")
                for premise in unmet:
                    self.assertTrue(premise.basis.strip())

    def test_nothing_is_advertised_above_its_evidence(self) -> None:
        for record in OBJECTIVE_REGISTRY:
            with self.subTest(family=record.family):
                if record.evidence_level in {
                    EvidenceLevel.NONE,
                    EvidenceLevel.CONTRACT_DECLARED,
                }:
                    self.assertIn(
                        record.support_state,
                        {
                            SupportState.IMPLEMENTATION_IN_PROGRESS,
                            SupportState.UNSUPPORTED_BY_CURRENT_DEPLOYMENT,
                        },
                    )
                if record.support_state is SupportState.OTA_LIVE_VERIFIED:
                    self.assertEqual(
                        record.evidence_level, EvidenceLevel.OTA_RAW_EVIDENCE
                    )
                    for reference in record.evidence_refs:
                        self.assertTrue(
                            (REPOSITORY_ROOT / reference).exists(),
                            f"retained evidence missing: {reference}",
                        )


class HonestyRuleTests(unittest.TestCase):
    """Each rule, shown biting on a record that breaks it."""

    def setUp(self) -> None:
        self.record = record_for("TrafficSteeringPreference")

    def _refused(self, **changes) -> str:
        with self.assertRaises(RegistryError) as raised:
            validate_record(replace(self.record, **changes))
        return str(raised.exception)

    def test_hardware_free_support_needs_a_hardware_free_run(self) -> None:
        # The dishonesty is stated explicitly rather than inherited from the
        # base record: at seam time the base sat below ROUND_TRIP, but once a
        # lane honestly completes the family the inherited level would satisfy
        # the guard and this test would stop testing anything.
        message = self._refused(
            support_state=SupportState.HARDWARE_FREE_VERIFIED,
            evidence_level=EvidenceLevel.CONTRACT_DECLARED,
        )
        self.assertIn("name and schema are not support", message)

    def test_hardware_free_support_needs_the_scenarios_named(self) -> None:
        message = self._refused(
            support_state=SupportState.HARDWARE_FREE_VERIFIED,
            evidence_level=EvidenceLevel.HARDWARE_FREE_ROUND_TRIP,
            hardware_free_scenarios=(),
        )
        self.assertIn("no scenario named", message)

    def test_a_hardware_free_run_cannot_be_recorded_as_ota_evidence(self) -> None:
        message = self._refused(
            support_state=SupportState.OTA_LIVE_VERIFIED,
            evidence_level=EvidenceLevel.HARDWARE_FREE_ROUND_TRIP,
            hardware_free_scenarios=("positive", "negative"),
        )
        self.assertIn("without OTA raw evidence", message)

    def test_ota_evidence_needs_retained_references(self) -> None:
        # Both halves stated, for the reason given on
        # test_hardware_free_support_needs_a_hardware_free_run: the base record
        # now carries real OTA references, so inheriting them would make this
        # assert nothing.
        # The support state is stated too, so the refusal comes from the
        # evidence-level rule this test names rather than from the
        # OTA_LIVE_VERIFIED rule, which would fire first and pass for the
        # wrong reason.
        message = self._refused(
            support_state=SupportState.HARDWARE_FREE_VERIFIED,
            evidence_level=EvidenceLevel.OTA_RAW_EVIDENCE,
            evidence_refs=(),
        )
        self.assertIn("no retained evidence", message)

    def test_ota_live_verified_is_refused_while_the_deployment_says_no(
        self,
    ) -> None:
        """Even otherwise complete OTA evidence cannot override capability."""
        record = record_for("SliceSLATarget")
        self.assertFalse(record.deployment_capability.submittable)
        with self.assertRaises(RegistryError) as raised:
            validate_record(
                replace(
                    record,
                    support_state=SupportState.OTA_LIVE_VERIFIED,
                    evidence_level=EvidenceLevel.OTA_RAW_EVIDENCE,
                    evidence_refs=("synthetic/slice-sla-ota-run.json",),
                )
            )
        self.assertIn(
            "OTA_LIVE_VERIFIED for an objective the deployment refuses",
            str(raised.exception),
        )

    def test_a_hardware_free_run_may_not_be_filed_as_an_evidence_reference(
        self,
    ) -> None:
        message = self._refused(
            support_state=SupportState.HARDWARE_FREE_VERIFIED,
            evidence_level=EvidenceLevel.HARDWARE_FREE_ROUND_TRIP,
            evidence_refs=("logs/mock-run.jsonl",),
        )
        self.assertIn("hardware-free run is recorded in", message)

    def test_an_unsubmittable_objective_states_why(self) -> None:
        message = self._refused(
            deployment_capability=DeploymentCapability(
                submittable=False,
                a1_policy_type_present=True,
                basis="probe",
            )
        )
        self.assertIn("states no blocking reason", message)

    def test_a_submittable_objective_needs_a_policy_type(self) -> None:
        message = self._refused(
            deployment_capability=DeploymentCapability(
                submittable=True,
                a1_policy_type_present=False,
                basis="probe",
            )
        )
        self.assertIn("no A1 policy type", message)

    def test_an_unmet_blocking_premise_forbids_a_support_claim(self) -> None:
        record = record_for("QoETarget")
        non_live_premises = tuple(replace(premise, live_only=False) for premise in record.premises)
        with self.assertRaises(RegistryError) as raised:
            validate_record(
                replace(
                    record,
                    support_state=SupportState.HARDWARE_FREE_VERIFIED,
                    evidence_level=EvidenceLevel.HARDWARE_FREE_ROUND_TRIP,
                    hardware_free_scenarios=("positive",),
                    premises=non_live_premises,
                )
            )
        self.assertIn("unmet premise", str(raised.exception))

    def test_a_live_only_gap_allows_hardware_free_but_not_ota(self) -> None:
        record = record_for("QoETarget")
        validate_record(record)
        with self.assertRaises(RegistryError):
            validate_record(replace(record, support_state=SupportState.OTA_VERIFICATION_PENDING))

    def test_a_missing_a1_policy_type_alone_still_allows_hardware_free_support(
        self,
    ) -> None:
        """The distinction the two axes exist for, stated as a passing case.

        Wire mapping v1.2 closed this premise for ``QoSTarget``, so construct
        the generic rule explicitly: absence blocks submission, not a completed
        hardware-free claim, where a missing measurement source would block it.
        """
        record = record_for("QoSTarget")
        missing_a1 = Premise(
            kind=PremiseKind.A1_POLICY_TYPE,
            statement="an A1 policy type exists for the selected wire mapping",
            met=False,
            basis="synthetic validator-rule fixture",
        )
        validate_record(
            replace(
                record,
                deployment_capability=DeploymentCapability(
                    submittable=False,
                    a1_policy_type_present=False,
                    basis="synthetic validator-rule fixture",
                    blocking_reasons=("NO_A1_POLICY_TYPE: synthetic fixture",),
                ),
                premises=(missing_a1,) + tuple(
                    premise
                    for premise in record.premises
                    if premise.kind is not PremiseKind.A1_POLICY_TYPE
                ),
                support_state=SupportState.HARDWARE_FREE_VERIFIED,
                evidence_level=EvidenceLevel.HARDWARE_FREE_ROUND_TRIP,
                hardware_free_scenarios=("positive", "negative", "fault"),
                # QoSTarget carries OTA references since 2026-08-28; this
                # fixture is a hardware-free record, so it carries none.
                evidence_refs=(),
            )
        )

    def test_a_record_naming_a_non_existent_interface_is_refused(self) -> None:
        mapping = replace(
            self.record.standard_mapping,
            notes="the enforcement report arrives over A2",
        )
        message = self._refused(standard_mapping=mapping)
        self.assertIn("non-existent A2 interface", message)

    def test_a_project_name_presented_as_a_published_term_is_refused(self) -> None:
        mapping = replace(
            self.record.standard_mapping,
            policy_interface="O-RAN TrafficSteeringPreference policy interface",
        )
        message = self._refused(standard_mapping=mapping)
        self.assertIn("as a published term", message)

    def test_an_unknown_family_is_refused(self) -> None:
        with self.assertRaises(RegistryError):
            validate_record(
                replace(
                    self.record,
                    family="LoadBalancing",
                    project_contract_id="objective/LoadBalancing",
                )
            )

    def test_a_bare_objective_name_is_not_a_project_identifier(self) -> None:
        message = self._refused(project_contract_id="TrafficSteeringPreference")
        self.assertIn("objective/<family>", message)

    def test_a_composite_cannot_outrank_its_weakest_component(self) -> None:
        # Build the contradiction from the component side so the test stays
        # meaningful after the composite is honestly completed: downgrade one
        # component below the composite instead of upgrading the composite
        # above a seam-time component.
        records = list(OBJECTIVE_REGISTRY)
        composite = record_for("QoSandTSP")
        component_name = composite.component_families[0]
        index = records.index(record_for(component_name))
        records[index] = replace(
            records[index],
            support_state=SupportState.IMPLEMENTATION_IN_PROGRESS,
            evidence_level=EvidenceLevel.CONTRACT_DECLARED,
            hardware_free_scenarios=(),
            # The component went OTA on 2026-08-28, so the downgrade has to
            # drop its raw-evidence references as well; leaving them would
            # trip the evidence-level rule before the composite rule under
            # test here.
            evidence_refs=(),
        )
        composite_index = records.index(composite)
        records[composite_index] = replace(
            composite,
            support_state=SupportState.HARDWARE_FREE_VERIFIED,
            evidence_level=EvidenceLevel.HARDWARE_FREE_ROUND_TRIP,
            hardware_free_scenarios=composite.hardware_free_scenarios
            or ("positive",),
            # The composite went OTA too, so its raw-evidence references go
            # with the downgrade for the same reason as the component's.
            evidence_refs=(),
        )
        with self.assertRaises(RegistryError) as raised:
            validate_registry(records)
        self.assertIn("separate passes may not be composed", str(raised.exception))

    def test_a_registry_missing_a_family_is_refused(self) -> None:
        with self.assertRaises(RegistryError) as raised:
            validate_registry([record for record in OBJECTIVE_REGISTRY][:3])
        self.assertIn("membership mismatch", str(raised.exception))

    def test_a_premise_without_a_basis_is_a_guess(self) -> None:
        message = self._refused(
            premises=(
                Premise(
                    kind=PremiseKind.MEASUREMENT_SOURCE,
                    statement="it works",
                    met=True,
                    basis="",
                ),
            )
        )
        self.assertIn("states no basis", message)


class PinToCellRegressionTests(unittest.TestCase):
    """The preserved contract is preserved, and related rather than renamed."""

    def setUp(self) -> None:
        from tests.assurance import pin_to_cell_support

        self.contracts = pin_to_cell_support.contract_set()
        self.support = pin_to_cell_support

    def test_the_frozen_contract_is_byte_for_byte_what_it_was(self) -> None:
        self.assertEqual(
            contract_content_hash(self.contracts["target"]), PIN_TARGET_CONTENT_HASH
        )

    def test_the_regression_contract_keeps_its_own_identifiers(self) -> None:
        target = self.contracts["target"]
        self.assertEqual(target.contract_id, "target/pin-to-cell")
        self.assertEqual(target.objective_family, PIN_REGRESSION_FAMILY)
        self.assertEqual(
            [predicate.predicate_id for predicate in target.predicates],
            ["serving-cell-is-pinned-throughout", "serving-cell-is-pinned-only"],
        )
        self.assertEqual(self.support.POLICY_TYPE_ID, "AIC_UECellSteering_1.0.0")

    def test_the_registry_did_not_rename_it_into_the_steering_family(self) -> None:
        record = record_for(PIN_REGRESSION_FAMILY)
        self.assertTrue(record.is_regression_contract)
        self.assertNotEqual(record.family, "TrafficSteeringPreference")
        self.assertNotIn(record.family, OBJECTIVE_FAMILIES)

    def test_the_relationship_is_stated_from_both_sides_and_versioned(self) -> None:
        pin = record_for(PIN_REGRESSION_FAMILY)
        steering = record_for("TrafficSteeringPreference")

        self.assertIn("TrafficSteeringPreference", pin.related_contracts)
        self.assertIn(PIN_REGRESSION_FAMILY, steering.related_contracts)
        for text in (
            pin.related_contracts["TrafficSteeringPreference"],
            steering.related_contracts[PIN_REGRESSION_FAMILY],
        ):
            self.assertIn("mapping version", text)
            self.assertIn("SEAMS-GATE4.md", text)

    def test_it_carries_real_ota_evidence_and_so_does_every_claimant(self) -> None:
        """It was the first family with OTA evidence; it is no longer the only one.

        Gate 5 stage 2 drove ``TrafficSteeringPreference`` and ``UELevelTarget``
        over real radio, so the interesting property is no longer a count.  What
        must stay true is that *every* record claiming OTA evidence has retained
        evidence that exists on disk -- the claim and the artifact together.
        """
        record = record_for(PIN_REGRESSION_FAMILY)
        self.assertEqual(record.support_state, SupportState.OTA_LIVE_VERIFIED)
        self.assertEqual(record.evidence_level, EvidenceLevel.OTA_RAW_EVIDENCE)
        self.assertTrue(record.as_dict()["evidenceIsOta"])

        claimants = [
            item.family
            for item in OBJECTIVE_REGISTRY
            if item.as_dict()["evidenceIsOta"]
        ]
        self.assertIn(PIN_REGRESSION_FAMILY, claimants)
        self.assertEqual(
            sorted(claimants),
            sorted(
                [
                    "QoSTarget",
                    "QoSandTSP",
                    "TrafficSteeringPreference",
                    "UELevelTarget",
                    PIN_REGRESSION_FAMILY,
                ]
            ),
        )
        for family in claimants:
            item = record_for(family)
            self.assertTrue(item.evidence_refs, f"{family} claims OTA with no refs")
            for reference in item.evidence_refs:
                self.assertTrue(
                    (REPOSITORY_ROOT / reference).exists(),
                    f"{family}: retained evidence missing: {reference}",
                )


def _strings(value, path=""):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.extend(_strings(item, f"{path}.{key}" if path else str(key)))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(_strings(item, f"{path}[{index}]"))
    elif isinstance(value, str):
        found.append((path, value))
    return found


if __name__ == "__main__":
    unittest.main()
