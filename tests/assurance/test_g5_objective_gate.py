"""Gate 5: the submission gate and the registry transition.

The gate's job is to refuse, and to refuse for the reason the Gate 4 registry
recorded rather than one it worked out for itself.  The transition planner's
job is to build what a record would become after an OTA run and to let the
registry's own validator reject it -- while writing nothing.

Stage 2 ran the two wire-mapped families OTA, so their live records now carry
``OTA_LIVE_VERIFIED``.  The rules below did not change; where a test used to
read its premise off one of those records, it now states the premise itself.
Inheriting a premise from a record that a lane may honestly move is how a test
quietly stops testing anything -- the same reasoning already written into
``HonestyRuleTests.test_hardware_free_support_needs_a_hardware_free_run``.
"""

from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path

from assurance.objectives import (
    FAMILY_MODULES,
    EvidenceLevel,
    RegistryError,
    SupportState,
    record_for,
    validate_record,
)
from tools.g5_registry_note import PREPARED_TRANSITIONS
from tools.g3ota.objectives import (
    ObjectiveRefused,
    plan_registry_transition,
    resolve_submittable_family,
    submission_readiness,
)

#: Evidence references are recorded repository-relative, so they are resolved
#: against the repository root rather than the test's working directory.
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class TheGateFollowsTheWireMapping(unittest.TestCase):
    """A family is accepted exactly when the versioned mapping gives it a kind."""

    def test_the_wire_mapped_families_are_accepted(self) -> None:
        from assurance.objectives.registry import ASSURANCE_FAMILY_TO_WIRE_KIND

        for family in sorted(set(ASSURANCE_FAMILY_TO_WIRE_KIND) & set(FAMILY_MODULES)):
            with self.subTest(family=family):
                module = resolve_submittable_family(family)
                self.assertEqual(module.family, family)

    def test_each_unmapped_family_is_refused_with_its_own_recorded_reason(self) -> None:
        from assurance.objectives.registry import ASSURANCE_FAMILY_TO_WIRE_KIND

        for family in sorted(set(FAMILY_MODULES) - set(ASSURANCE_FAMILY_TO_WIRE_KIND)):
            with self.subTest(family=family):
                with self.assertRaises(ObjectiveRefused) as caught:
                    resolve_submittable_family(family)
                message = str(caught.exception)
                record = record_for(family)
                # The gate reads the FAMILY MODULE's lifecycle for "is there a
                # policy type at all", and the REGISTRY for "will the deployment
                # take it".  The two disagree for some families -- QoEandTSP's
                # module names a policy type its registry record does not -- so
                # the assertion has to key on the same half the gate does.
                readiness = submission_readiness(family)
                if readiness["familyModulePolicyTypeId"] is None:
                    self.assertIn("no policy type", message)
                else:
                    self.assertTrue(
                        any(r.split(":")[0] in message
                            for r in record.deployment_capability.blocking_reasons),
                        f"refusal did not carry a recorded reason: {message}",
                    )

    def test_an_unknown_family_is_refused_by_name(self) -> None:
        with self.assertRaises(ObjectiveRefused):
            resolve_submittable_family("NoSuchObjective")

    def test_readiness_reports_both_halves_without_deciding(self) -> None:
        accepted = submission_readiness("TrafficSteeringPreference")
        self.assertTrue(accepted["registrySubmittable"])
        self.assertEqual(
            accepted["familyModulePolicyTypeId"], "AIC_UECellSteering_1.0.0"
        )
        self.assertFalse(accepted["blockingReasons"])

        for family in ("QoSTarget", "QoSandTSP"):
            with self.subTest(family=family):
                accepted = submission_readiness(family)
                self.assertTrue(accepted["registrySubmittable"])
                self.assertEqual(
                    accepted["familyModulePolicyTypeId"],
                    "AIC_UECellSteering_1.0.0",
                )
                self.assertFalse(accepted["blockingReasons"])


class ThePreparedTransitionIsPreparedNotApplied(unittest.TestCase):

    def test_pending_is_reachable_from_hardware_free_verified(self) -> None:
        """The first hop, stated rather than read off a record.

        No family is still sitting at ``HARDWARE_FREE_VERIFIED`` *and*
        submittable -- the two that were went OTA -- so the premise is built
        here.  What is under test is the validator's rule, not today's registry.
        """
        for family in ("TrafficSteeringPreference", "UELevelTarget"):
            with self.subTest(family=family):
                before = dataclasses.replace(
                    record_for(family),
                    support_state=SupportState.HARDWARE_FREE_VERIFIED,
                    evidence_level=EvidenceLevel.HARDWARE_FREE_ROUND_TRIP,
                    evidence_refs=(),
                )
                validate_record(before)
                after = dataclasses.replace(
                    before, support_state=SupportState.OTA_VERIFICATION_PENDING
                )
                validate_record(after)
                self.assertEqual(
                    after.support_state, SupportState.OTA_VERIFICATION_PENDING
                )
                self.assertEqual(
                    after.evidence_level, EvidenceLevel.HARDWARE_FREE_ROUND_TRIP
                )

    def test_planning_changes_nothing_in_the_live_registry(self) -> None:
        before = record_for("TrafficSteeringPreference")
        plan_registry_transition(
            "TrafficSteeringPreference",
            to_state=SupportState.OTA_VERIFICATION_PENDING,
        )
        self.assertEqual(record_for("TrafficSteeringPreference"), before)

    def test_ota_live_verified_is_refused_without_evidence_references(self) -> None:
        # Stated explicitly: the live record now *has* references, and
        # ``plan_registry_transition`` carries the current ones forward when
        # none are supplied, so an empty argument no longer describes an empty
        # record.  The rule is about the record.
        stripped = dataclasses.replace(
            record_for("TrafficSteeringPreference"),
            support_state=SupportState.OTA_LIVE_VERIFIED,
            evidence_level=EvidenceLevel.OTA_RAW_EVIDENCE,
            evidence_refs=(),
        )
        with self.assertRaises(RegistryError) as raised:
            validate_record(stripped)
        self.assertIn("retained evidence references", str(raised.exception))

    def test_qos_went_ota_and_carries_the_evidence_that_permits_it(self) -> None:
        """The QoS pair ran OTA on 2026-08-28; the state says so.

        This test previously pinned both families at
        ``HARDWARE_FREE_VERIFIED`` because no QoS OTA run existed.  One does
        now, so the assertion is the new fact rather than a relaxed version
        of the old one: the claim is only permitted while retained references
        back it, and every one of them has to be a file on disk.
        """
        for family in ("QoSTarget", "QoSandTSP"):
            with self.subTest(family=family):
                record = record_for(family)
                self.assertEqual(
                    record.support_state, SupportState.OTA_LIVE_VERIFIED)
                self.assertEqual(
                    record.evidence_level, EvidenceLevel.OTA_RAW_EVIDENCE
                )
                self.assertTrue(record.evidence_refs)
                for reference in record.evidence_refs:
                    self.assertTrue(
                        (REPOSITORY_ROOT / reference).exists(),
                        f"{family} names missing evidence {reference}",
                    )
                # The state is refused the moment the references go away.
                with self.assertRaises(RegistryError):
                    validate_record(
                        dataclasses.replace(record, evidence_refs=())
                    )

    def test_ota_live_verified_can_be_planned_once_evidence_exists(self) -> None:
        """For a wire-mapped family, evidence references are the last gate."""
        candidate, diff = plan_registry_transition(
            "TrafficSteeringPreference",
            to_state=SupportState.OTA_LIVE_VERIFIED,
            evidence_refs=("docs/integration/evidence/GATE5-OTA-example.json",),
        )
        self.assertEqual(candidate.support_state, SupportState.OTA_LIVE_VERIFIED)
        self.assertEqual(candidate.evidence_level, EvidenceLevel.OTA_RAW_EVIDENCE)
        self.assertEqual(diff["evidenceLevel"][1], "OTA_RAW_EVIDENCE")

    def test_the_prepared_note_named_the_families_that_went_on_to_go_ota(
        self,
    ) -> None:
        """Stage 1's prepared map, checked against what stage 2 actually did.

        The map is a historical statement -- "these are the families whose
        actuator and evidence counter this deployment really has" -- and the
        way to keep it honest is to require that each named family reached
        ``OTA_LIVE_VERIFIED``, not that it is still merely pending.
        """
        for family, to_state in PREPARED_TRANSITIONS.items():
            with self.subTest(family=family):
                self.assertEqual(to_state, SupportState.OTA_VERIFICATION_PENDING)
                record = record_for(family)
                self.assertEqual(
                    record.support_state, SupportState.OTA_LIVE_VERIFIED)
                self.assertEqual(
                    record.evidence_level, EvidenceLevel.OTA_RAW_EVIDENCE)
                self.assertTrue(record.evidence_refs)


if __name__ == "__main__":
    unittest.main()
