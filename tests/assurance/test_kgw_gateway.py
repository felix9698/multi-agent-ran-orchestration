"""The Write Gateway's normal path, and what it refuses to do on the way.

Design authority: ``docs/superpowers/specs/2026-08-20-unified-ota-assurance-system-design.md``
sections 4.4, 7 and 9; ``task_0820_unified_ota_assurance_system.md`` sections
3.6, 6 and 7.  Ownership: ``docs/architecture/SEAMS-GATE2.md`` lane KGW.

Hermetic: an in-memory configuration store, an injected clock, no socket, no
model client, no testbed (design section 15 -- Gate 2 reports zero live
E2/RAN/OTA/USRP calls).
"""

from __future__ import annotations

import inspect
import unittest

from assurance.contracts.capability import ActuatorPath
from assurance.gateway.commands import SIDE_EFFECT_FREE_OPERATIONS, GatewayOperation
from assurance.gateway.journal import NextSafeAction, TransactionPhase
from assurance.gateway.mock_adapter import FaultInjection, MockActuationAdapter
from assurance.gateway.registry import GatewayAdapterRegistry
from assurance.gateway.token import TokenKind
from assurance.gateway.write_gateway import (
    GatewayOutcome,
    GatewayRefusal,
    WriteGateway,
)
from tests.assurance.kgw_support import (
    APPLIED,
    APPLIED_HASH,
    BASELINE,
    BASELINE_HASH,
    PLAN,
    GatewayFixture,
    token,
)


class PrepareHasNoSideEffect(GatewayFixture, unittest.TestCase):
    """Task section 6.2: no real configuration change before READY."""

    def setUp(self):
        self.build()

    def test_prepare_stages_without_writing_anything(self):
        result = self.do_prepare()
        self.assertIs(result.outcome, GatewayOutcome.ACKED)
        self.assertEqual(self.adapter.writes, [])
        self.assertEqual(self.adapter.snapshot(), dict(BASELINE))
        self.assertEqual(result.observed_config_hash, BASELINE_HASH)

    def test_prepare_dispatches_only_side_effect_free_operations(self):
        self.do_prepare()
        operations = {GatewayOperation(c["operation"]) for c in self.adapter.commands}
        self.assertTrue(operations <= SIDE_EFFECT_FREE_OPERATIONS, operations)

    def test_a_staged_plan_is_durable_and_names_its_next_safe_action(self):
        self.do_prepare()
        record = self.gateway.transaction_record("tx-1")
        self.assertIs(record.phase, TransactionPhase.PREPARED)
        self.assertIs(record.next_safe_action, NextSafeAction.READY)
        self.assertEqual(record.baseline_config_hash, BASELINE_HASH)
        self.assertEqual(record.applied_config_hash, APPLIED_HASH)
        self.assertEqual(record.reservations["adapter"], "mock")

    def test_a_prepare_failure_leaves_nothing_to_roll_back(self):
        self.build(faults=FaultInjection(fail_prepare=True))
        result = self.do_prepare()
        self.assertIs(result.outcome, GatewayOutcome.REJECTED)
        self.assertEqual(self.adapter.writes, [])
        self.assertIsNone(self.gateway.transaction_record("tx-1"))
        # And the trial cannot proceed past it.
        self.assertIs(self.do_ready().outcome, GatewayOutcome.REJECTED)
        self.assertIs(self.do_commit().outcome, GatewayOutcome.REJECTED)

    def test_a_plan_outside_the_contracted_surface_is_refused(self):
        plan = dict(PLAN, baselineConfig={"servingCell": "cell-1"},
                    steps=[{"axis": "servingCell", "value": "cell-2"}])
        result = self.do_prepare(plan)
        self.assertIs(result.outcome, GatewayOutcome.REJECTED)
        self.assertIn("configuration surface", result.detail)
        self.assertEqual(self.adapter.commands, [])

    def test_a_malformed_plan_is_refused_before_anything_is_staged(self):
        for broken in (
            dict(PLAN, steps=[]),
            dict(PLAN, steps=[{"axis": "servingCell", "value": "a"},
                              {"axis": "servingCell", "value": "b"}]),
            dict(PLAN, steps=[{"axis": "unknownAxis", "value": 1}]),
            dict(PLAN, extraField=1),
            {"adapter": "mock"},
        ):
            with self.subTest(plan=sorted(broken)):
                self.build()
                result = self.do_prepare(broken)
                self.assertIs(result.outcome, GatewayOutcome.REJECTED)
                self.assertIsNone(self.gateway.transaction_record("tx-1"))
                self.assertEqual(self.adapter.commands, [])

    def test_staging_over_a_live_change_is_refused(self):
        self.build()
        self.commit_applied()
        result = self.do_prepare(fence=2)
        self.assertIs(result.outcome, GatewayOutcome.REJECTED)
        self.assertIn("may already exist", result.detail)
        record = self.gateway.transaction_record("tx-1")
        self.assertIs(record.phase, TransactionPhase.APPLIED)
        self.assertEqual(record.applied_axes, ("servingCell", "queuePriority"))

    def test_a_plan_whose_baseline_is_not_the_permits_configuration_is_refused(self):
        result = self.do_prepare(expected=APPLIED_HASH)
        self.assertIs(result.outcome, GatewayOutcome.REJECTED_CONFIG_MISMATCH)
        self.assertEqual(self.adapter.commands, [])


class TheCommitLine(GatewayFixture, unittest.TestCase):
    """Design section 7: apply only after a durable ready and commit decision."""

    def setUp(self):
        self.build()

    def test_the_canonical_path_applies_and_finalizes(self):
        results = self.commit_applied()
        self.assertIs(results["prepare"].outcome, GatewayOutcome.ACKED)
        self.assertIs(results["ready"].outcome, GatewayOutcome.ACKED)
        self.assertIs(results["commit"].outcome, GatewayOutcome.ACKED)
        self.assertEqual(self.adapter.snapshot(), APPLIED)
        record = self.gateway.transaction_record("tx-1")
        self.assertIs(record.phase, TransactionPhase.APPLIED)
        self.assertIs(record.next_safe_action, NextSafeAction.FINALIZE_LIVE)

        final = self.do_finalize()
        self.assertIs(final.outcome, GatewayOutcome.ACKED)
        record = self.gateway.transaction_record("tx-1")
        self.assertIs(record.phase, TransactionPhase.FINALIZED)
        self.assertIs(record.next_safe_action, NextSafeAction.SETTLE)
        self.assertIsNone(record.watchdog_deadline)

    def test_commit_without_a_durable_ready_is_refused(self):
        self.do_prepare()
        result = self.do_commit()
        self.assertIs(result.outcome, GatewayOutcome.REJECTED)
        self.assertIn("READY", result.detail)
        self.assertEqual(self.adapter.writes, [])

    def test_ready_is_durable_and_still_writes_nothing(self):
        self.do_prepare()
        self.do_ready()
        record = self.gateway.transaction_record("tx-1")
        self.assertIs(record.phase, TransactionPhase.READY)
        self.assertIs(record.next_safe_action, NextSafeAction.COMMIT)
        self.assertEqual(self.adapter.writes, [])

    def test_the_watchdog_is_armed_at_apply_and_disarmed_at_finalize(self):
        self.commit_applied()
        self.assertEqual(
            self.gateway.transaction_record("tx-1").watchdog_deadline,
            token(TokenKind.COMMIT).lease_expiry,
        )
        self.do_finalize()
        self.assertIsNone(self.gateway.transaction_record("tx-1").watchdog_deadline)

    def test_every_command_carries_a_distinct_derived_idempotency_key(self):
        self.commit_applied()
        self.do_finalize()
        keys = self.adapter.idempotency_keys
        self.assertEqual(len(keys), len(set(keys)), keys)


class FinalizeNeedsRereadAndAcknowledgement(GatewayFixture, unittest.TestCase):
    """Task section 6.9: reread and finalize ack before a success settlement."""

    def test_a_missing_finalize_acknowledgement_is_not_a_success(self):
        self.build()
        self.commit_applied()
        self.adapter.set_faults(FaultInjection(drop_finalize_ack=True))
        result = self.do_finalize()
        self.assertIs(result.outcome, GatewayOutcome.UNKNOWN)
        record = self.gateway.transaction_record("tx-1")
        self.assertIs(record.phase, TransactionPhase.APPLIED)
        self.assertIs(record.next_safe_action, NextSafeAction.FINALIZE_LIVE)

    def test_a_drifted_configuration_cannot_be_finalized(self):
        self.build()
        self.commit_applied()
        self.adapter.apply_drift({"prbCap": 51})
        result = self.do_finalize()
        self.assertIs(result.outcome, GatewayOutcome.REJECTED_CONFIG_MISMATCH)
        self.assertIsNot(
            self.gateway.transaction_record("tx-1").phase, TransactionPhase.FINALIZED
        )

    def test_finalize_before_an_applied_change_is_refused(self):
        self.build()
        self.do_prepare()
        self.do_ready()
        result = self.do_finalize()
        self.assertIs(result.outcome, GatewayOutcome.REJECTED)
        self.assertIn("applied change", result.detail)


class TheAdapterRegistryIsTheBoundary(unittest.TestCase):
    """Design section 9: the boundary is enforced where adapters are admitted."""

    class LabSetupAdapter:
        actuator_path = ActuatorPath.LAB_SETUP_PREPARATION

        def dispatch(self, *, token, command):  # pragma: no cover - never called
            raise AssertionError("a Lab Setup adapter must never be dispatched to")

    def test_a_lab_setup_adapter_cannot_be_registered(self):
        registry = GatewayAdapterRegistry()
        with self.assertRaises(GatewayRefusal) as raised:
            registry.register("labsetup", self.LabSetupAdapter())
        self.assertIn("OFFICIAL_ORAN_DYNAMIC", str(raised.exception))
        self.assertEqual(registry.registered_paths(), {})

    def test_a_name_cannot_be_silently_replaced(self):
        registry = GatewayAdapterRegistry()
        first = MockActuationAdapter(config=BASELINE)
        registry.register("mock", first)
        with self.assertRaises(GatewayRefusal):
            registry.register("mock", MockActuationAdapter(config=BASELINE))
        self.assertIs(registry.resolve("mock"), first)

    def test_an_absent_adapter_refuses_rather_than_returning_none(self):
        with self.assertRaises(GatewayRefusal):
            GatewayAdapterRegistry().resolve("missing")

    def test_registered_paths_report_only_the_official_path(self):
        registry = GatewayAdapterRegistry()
        registry.register("mock", MockActuationAdapter(config=BASELINE))
        self.assertEqual(
            registry.registered_paths(), {"mock": ActuatorPath.OFFICIAL_ORAN_DYNAMIC}
        )


class TheGatewayImplementsTheFrozenProtocol(GatewayFixture, unittest.TestCase):

    def test_every_frozen_operation_is_implemented_with_its_frozen_signature(self):
        self.build()
        for name in (
            "prepare", "ready", "commit", "stop", "reverse_rollback",
            "reread_configuration", "confirm_recovery", "emergency_safe_state",
            "finalize_live", "query_transaction",
        ):
            with self.subTest(operation=name):
                frozen = inspect.signature(getattr(WriteGateway, name))
                implemented = inspect.signature(getattr(type(self.gateway), name))
                self.assertEqual(str(frozen), str(implemented))

    def test_the_gateway_reports_only_names_and_paths_of_its_adapters(self):
        self.build()
        self.assertEqual(
            self.gateway.registered_paths(),
            {"mock": ActuatorPath.OFFICIAL_ORAN_DYNAMIC},
        )


if __name__ == "__main__":
    unittest.main()
