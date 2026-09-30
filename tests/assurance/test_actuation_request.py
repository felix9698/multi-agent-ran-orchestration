"""Where the policy body's numbers come from, and why they are the same twice.

Gate 3 binds the R1 adapter's ``policy_builder`` to the project's real
``translate_intent``.  That function needs fourteen behaviour-bearing values a
gateway command does not carry, and its context object says outright that it
has "intentionally no convenience defaults: the contract forbids silently
introducing behavior-critical defaults".

So this file checks two things.  First, that every one of those values is
derived from something the epoch froze --
:func:`assurance.contracts.actuation_request.derive_actuation_request` refuses
where a contract is silent instead of filling in a number.  Second, that the
derivation is *pure*: the same frozen contracts and the same Kernel
identifiers produce the same request, UUIDs included, because a drawn
identifier would put a random value in the event stream and the terminal state
hash would stop reproducing on replay.

The last class is the payoff: the derived values are fed to the real frozen
translator and the result is validated against the frozen
``AIC_UECellSteering_1.0.0`` policy schema.  Still no live call.
"""

from __future__ import annotations

import json
import socket
import unittest
from dataclasses import replace
from typing import Any, Dict, Mapping
from unittest.mock import patch

from oran.rapp.contract_support import _bundle_dir, validate
from oran.rapp.policy_translator import (
    POLICY_TYPE_ID,
    PolicyTranslationContext,
    translate_intent,
)

from assurance.contracts.actuation_request import (
    ACTUATION_DERIVATION_RULES,
    ActuationDerivationError,
    derive_actuation_request,
)
from assurance.core.addressing import deterministic_uuid

from tests.assurance.pin_to_cell_support import (
    CASE_ID,
    TARGET_NCI,
    PinToCellFixture,
    contract_set,
)

ISSUED_AT = "2026-08-21T09:00:00.000000Z"
LEASE_EXPIRY = "2026-08-21T09:00:30.000000Z"
TRIAL_ID = f"{CASE_ID}:trial:1"


def setUpModule():
    """No test in this module may open a socket."""

    def refuse(*args, **kwargs):
        raise AssertionError("a Gate 3 preparation test attempted a live call")

    patcher = patch.object(socket, "socket", refuse)
    patcher.start()
    globals()["_socket_patcher"] = patcher


def tearDownModule():
    globals()["_socket_patcher"].stop()


class DerivationFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.data = contract_set()
        fixture = PinToCellFixture()
        fixture.build()
        self.candidate = fixture.kernel.current_catalog().candidates[0]

    def derive(self, **overrides: Any):
        arguments: Dict[str, Any] = {
            "target": self.data["target"],
            "candidate": self.candidate,
            "actuator": self.data["actuator"],
            "harm": self.data["harm"],
            "measurements": (
                self.data["measurement_min"],
                self.data["measurement_max"],
            ),
            "case_policy": self.data["case_policy"],
            "case_id": CASE_ID,
            "trial_id": TRIAL_ID,
            "trial_index": 1,
            "issued_at": ISSUED_AT,
            "lease_expiry": LEASE_EXPIRY,
        }
        arguments.update(overrides)
        return derive_actuation_request(**arguments)


class EveryValueHasAFrozenSource(DerivationFixture):
    def test_the_enforced_timeout_is_the_action_deadline_and_the_pacing_floor(
        self,
    ) -> None:
        """The runtime timeout that makes the harm bound true, used as itself.

        A second actuation inside that window would begin before the bound
        that justified the first one had a chance to hold, so the minimum
        spacing is the same number rather than a separate one nobody
        contracted.
        """
        bound = self.data["harm"].bounds[0]

        request = self.derive()

        self.assertEqual(request.action_deadline_ms, bound.enforced_timeout_ms)
        self.assertEqual(request.rollback_timeout_ms, bound.enforced_timeout_ms)
        self.assertEqual(
            request.min_seconds_between_actuations,
            -(-bound.enforced_timeout_ms // 1000),
        )
        self.assertEqual(
            request.derivation_rules["actionDeadlineMs"],
            "harm_bound_enforced_timeout",
        )

    def test_the_kpi_freshness_is_the_strictest_the_epoch_requires(self) -> None:
        loose = replace(self.data["measurement_max"], freshness_bound_ms=9_000)

        request = self.derive(measurements=(self.data["measurement_min"], loose))

        self.assertEqual(
            request.required_kpi_freshness_ms,
            self.data["measurement_min"].freshness_bound_ms,
        )

    def test_validity_is_the_permit_lease_and_not_a_second_clock(self) -> None:
        request = self.derive()

        self.assertEqual(request.not_before, ISSUED_AT)
        self.assertEqual(request.expires_at, LEASE_EXPIRY)

    def test_the_change_itself_comes_from_the_frozen_candidate(self) -> None:
        request = self.derive()

        self.assertEqual(request.parameters, {"servingCell": str(TARGET_NCI)})
        self.assertEqual(request.objective_family, "UeCellSteeringPinToCell")
        self.assertEqual(request.policy_type_id, POLICY_TYPE_ID)
        self.assertEqual(request.actuator_path.value, "OFFICIAL_ORAN_DYNAMIC")

    def test_the_producer_is_the_kernel_and_carries_no_authority(self) -> None:
        request = self.derive()

        self.assertEqual(request.producer_id, "ASSURANCE_KERNEL")
        self.assertEqual(request.intent_revision, 1)
        self.assertEqual(request.policy_revision, 1)

    def test_the_recorded_rules_name_every_computed_field(self) -> None:
        request = self.derive()

        self.assertEqual(request.derivation_rules, dict(ACTUATION_DERIVATION_RULES))


class NothingIsDrawn(DerivationFixture):
    def test_the_same_inputs_derive_the_same_request_including_uuids(self) -> None:
        first = self.derive()
        second = self.derive()

        self.assertEqual(first, second)
        self.assertEqual(first.to_canonical_dict(), second.to_canonical_dict())

    def test_the_uuids_are_functions_of_the_kernel_identifiers(self) -> None:
        request = self.derive()

        self.assertEqual(request.intent_id, deterministic_uuid(CASE_ID))
        self.assertEqual(
            request.correlation_id, deterministic_uuid(f"{CASE_ID}:{TRIAL_ID}")
        )

    def test_a_second_trial_correlates_differently(self) -> None:
        first = self.derive()
        second = self.derive(trial_id=f"{CASE_ID}:trial:2", trial_index=2)

        # Same intent, different trial: one case, two attempts.
        self.assertEqual(second.intent_id, first.intent_id)
        self.assertNotEqual(second.correlation_id, first.correlation_id)
        self.assertEqual(second.policy_revision, 2)

    def test_the_downstream_idempotency_key_is_correlated_not_coincidental(
        self,
    ) -> None:
        """Two namespaces, one origin.

        The gateway deduplicates on its own per-command key; the Near-RT RIC
        deduplicates on the policy body's.  They are different strings on
        purpose -- a retried command is not a new policy revision -- but both
        are functions of the same Kernel identifiers, so a body and the
        commands that carry it can be tied together after the fact.
        """
        first = self.derive()
        again = self.derive()

        self.assertEqual(first.idempotency_key, f"{first.intent_id}:1")
        self.assertEqual(again.idempotency_key, first.idempotency_key)
        self.assertNotEqual(
            first.idempotency_key, f"gateway:tx:{TRIAL_ID}:0:COMMIT"
        )

    def test_joint_policies_are_distinct_but_retries_keep_their_identity(self):
        first = self.derive(policy_scope="servingCell@131")
        second = self.derive(policy_scope="servingCell@132")
        retry = self.derive(policy_scope="servingCell@131")
        later = self.derive(policy_scope="servingCell@131",
                            trial_id=f"{CASE_ID}:trial:2", trial_index=2)
        self.assertNotEqual(first.idempotency_key, second.idempotency_key)
        self.assertEqual(first, retry)
        self.assertEqual(first.intent_id, later.intent_id)
        self.assertNotEqual(first.idempotency_key, later.idempotency_key)
        self.assertEqual(first.correlation_id, second.correlation_id)


class SilenceIsRefusedNotDefaulted(DerivationFixture):
    def test_a_harm_bound_with_no_enforced_timeout_is_refused(self) -> None:
        bound = replace(self.data["harm"].bounds[0], enforced_timeout_ms=0)
        harm = replace(self.data["harm"], bounds=(bound,))

        with self.assertRaises(ActuationDerivationError) as refusal:
            self.derive(harm=harm)

        self.assertIn("enforced timeout", str(refusal.exception))

    def test_no_measurement_freshness_is_refused(self) -> None:
        with self.assertRaises(ActuationDerivationError):
            self.derive(measurements=())

    def test_an_off_path_actuator_is_refused(self) -> None:
        from assurance.contracts import ActuatorPath

        with self.assertRaises(ActuationDerivationError) as refusal:
            self.derive(
                actuator=replace(
                    self.data["actuator"], path=ActuatorPath.LAB_SETUP_PREPARATION
                )
            )

        self.assertIn("official dynamic path", str(refusal.exception))

    def test_a_harm_contract_the_case_does_not_run_under_is_refused(self) -> None:
        with self.assertRaises(ActuationDerivationError):
            self.derive(
                case_policy=replace(
                    self.data["case_policy"], harm_contract_refs=("harm/other",)
                )
            )

    def test_a_candidate_from_another_target_is_refused(self) -> None:
        stray = replace(self.candidate, target_ref="target/somewhere-else")

        with self.assertRaises(ActuationDerivationError):
            self.derive(candidate=stray)

    def test_a_non_utc_permit_instant_is_refused(self) -> None:
        with self.assertRaises(ActuationDerivationError):
            self.derive(issued_at="2026-08-21 09:00:00")


class TheDerivedValuesBuildTheFrozenPolicyBody(DerivationFixture):
    """The proof: these values, the real translator, the frozen schema."""

    @classmethod
    def setUpClass(cls) -> None:
        bundle = _bundle_dir()
        cls.golden = json.loads(
            (bundle / "golden/golden-vectors.1.0.0.json").read_text(encoding="utf-8")
        )["canonicalObjects"]

    def build_policy(self, request) -> Mapping[str, Any]:
        """Shape the derived values the way the A1 interface wants them.

        This is the composition root's half, and it lives here rather than in
        ``assurance/`` because the seam forbids that package from importing
        ``oran.rapp``.  Only *shaping* happens: the PLMN comes from the
        deployment's own material and the cell identity comes from the frozen
        candidate, which is exactly why the candidate carries ``nCI`` as an
        integer.
        """
        from decision.intent_model import (
            ConstraintType,
            Intent,
            IntentPriority,
            IntentScope,
            IntentTarget,
            IntentType,
        )

        plmn = self.golden["policy"]["steeringObjective"]["actionEnvelope"][
            "allowedCells"
        ][0]["plmnId"]
        intent = Intent(
            id=request.intent_id,
            type=IntentType.THROUGHPUT_GOAL,
            target=IntentTarget("throughput", ConstraintType.MIN, 1.0, unit="Mbps"),
            scope=IntentScope(ue_ids=["ue-1"]),
            priority=IntentPriority.HIGH,
        )
        context = PolicyTranslationContext(
            ue_id=self.golden["policy"]["scope"]["ueId"],
            allowed_cells=[
                {
                    "plmnId": plmn,
                    "cId": {"ncI": int(request.parameters["servingCell"])},
                }
            ],
            forbidden_cells=[],
            objective_kind="PIN_TO_CELL",
            improvement_threshold_prb=None,
            min_seconds_between_actuations=request.min_seconds_between_actuations,
            required_kpi_freshness_ms=request.required_kpi_freshness_ms,
            action_deadline_ms=request.action_deadline_ms,
            not_before=request.not_before,
            expires_at=request.expires_at,
            rollback_on=["READBACK_MISMATCH", "APPLY_FAILED"],
            rollback_timeout_ms=request.rollback_timeout_ms,
            intent_revision=request.intent_revision,
            policy_revision=request.policy_revision,
            correlation_id=request.correlation_id,
            producer_id="assurance-kernel",
            intent_id=request.intent_id,
        )
        return translate_intent(
            intent,
            policy_type_discovery={
                "policyTypeIds": [POLICY_TYPE_ID],
                "policyTypeObject": {
                    "policyTypeId": POLICY_TYPE_ID,
                    "policySchema": json.loads(
                        (
                            _bundle_dir()
                            / "AIC_UECellSteering_1.0.0.policy.schema.json"
                        ).read_text(encoding="utf-8")
                    ),
                    "statusSchema": json.loads(
                        (
                            _bundle_dir()
                            / "AIC_UECellSteering_1.0.0.status.schema.json"
                        ).read_text(encoding="utf-8")
                    ),
                },
            },
            capability_manifest=self.golden["capabilityManifest"],
            context=context,
        )

    def test_the_derived_values_produce_a_schema_valid_pin_to_cell_policy(
        self,
    ) -> None:
        request = self.derive()

        policy = self.build_policy(request)

        validate(policy, "AIC_UECellSteering_1.0.0.policy")
        self.assertEqual(policy["steeringObjective"]["kind"], "PIN_TO_CELL")
        self.assertEqual(
            policy["steeringObjective"]["actionEnvelope"]["allowedCells"],
            [
                {
                    "plmnId": self.golden["policy"]["steeringObjective"][
                        "actionEnvelope"
                    ]["allowedCells"][0]["plmnId"],
                    "cId": {"ncI": TARGET_NCI},
                }
            ],
        )
        self.assertNotIn("improvementThresholdPrb", policy["steeringObjective"])

    def test_every_behaviour_bearing_policy_value_came_from_the_derivation(
        self,
    ) -> None:
        request = self.derive()

        policy = self.build_policy(request)

        self.assertEqual(
            policy["constraints"],
            {
                "maxActuationsPerEpisode": 1,
                "minSecondsBetweenActuations": request.min_seconds_between_actuations,
                "requiredKpiFreshnessMs": request.required_kpi_freshness_ms,
                "actionDeadlineMs": request.action_deadline_ms,
            },
        )
        self.assertEqual(
            policy["validity"],
            {"notBefore": request.not_before, "expiresAt": request.expires_at},
        )
        self.assertEqual(
            policy["rollbackPolicy"]["timeoutMs"], request.rollback_timeout_ms
        )
        self.assertEqual(policy["trace"]["intentId"], request.intent_id)
        self.assertEqual(policy["trace"]["correlationId"], request.correlation_id)
        self.assertEqual(
            policy["trace"]["idempotencyKey"], request.idempotency_key
        )

    def test_the_same_trial_builds_the_same_policy_body_twice(self) -> None:
        first = self.build_policy(self.derive())
        second = self.build_policy(self.derive())

        self.assertEqual(
            json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True)
        )


if __name__ == "__main__":
    unittest.main()
