"""The official write path, end to end, for each Campaign 5 family.

    XAppExecutionPlan -> Kernel permit -> ActuationPlan(adapter="r1-<family>")
    -> TokenBoundWriteGateway.prepare/ready/commit -> R1Adapter.dispatch
    -> policy_builder -> A1 policy -> A1-P producer -> readback

With a working configuration counter the whole chain ACKs and a policy is
created; with the counter absent (today's deployed binary) the gateway cannot
even establish the baseline, so it stages nothing and reports UNKNOWN -- never a
fabricated effect.
"""

from __future__ import annotations

import unittest

from assurance.contracts.capability import ActuatorPath
from assurance.gateway.write_gateway import GatewayOutcome, GatewayRefusal
from assurance.gateway.gateway import TokenBoundWriteGateway
from assurance.gateway.plan import config_hash

from oran.campaign5.builders import (
    Campaign5BuilderError,
    fixed_validity,
    make_policy_builder,
)
from oran.campaign5.families import CAMPAIGN5_FAMILIES
from oran.campaign5.producer import Campaign5PolicyProducer
from oran.campaign5.readback import AbsentCounterReader, DictKpmConfigReader
from tools.campaign5.route import (
    build_official_adapter,
    build_official_gateway,
    official_plan,
    run_official_route,
)
from tests.assurance.campaign5_support import (
    CLOCK,
    FAMILY_CASES,
    VALIDITY,
    ProducerPolicyPort,
    family,
    permit,
)


def _route(fam_key, *, counter_present, gnb_applies=True):
    fam = family(fam_key)
    case = FAMILY_CASES[fam_key]
    producer = Campaign5PolicyProducer()
    kpm = DictKpmConfigReader()
    if counter_present:
        kpm.publish(fam.readback_counter, case["scope"], case["baseline"])
        reader = kpm
    else:
        reader = AbsentCounterReader()
    port = ProducerPolicyPort(producer, fam, kpm, gnb_applies=gnb_applies,
                              publish_counter=counter_present)
    adapter = build_official_adapter(
        fam, policy_port=port, validity_provider=fixed_validity(*VALIDITY),
        kpm_reader=reader, cadence_ms=10, deadline_ms=50,
    )
    gateway = build_official_gateway(
        fam, adapter, safe_state={fam.axis: case["baseline"]}, clock=lambda: CLOCK,
    )
    plan = official_plan(fam, scope=case["scope"], baseline=case["baseline"],
                         target=case["target"])
    outcomes = run_official_route(gateway, plan, permit=permit(fence=0))
    return fam, adapter, plan, outcomes


class OfficialRouteWithAWorkingCounter(unittest.TestCase):
    def test_first_kernel_fence_zero_passes_prepare(self):
        _, _, _, outcomes = _route("cap", counter_present=True)
        self.assertEqual(outcomes["prepare"].outcome, GatewayOutcome.ACKED)

    def test_prepare_ready_commit_ack_for_every_family_with_an_available_readback(self):
        for key in FAMILY_CASES:
            fam, adapter, plan, outcomes = _route(key, counter_present=True)
            with self.subTest(family=key):
                if key == "power":
                    # The three power distBinX values have no JSONL label, so
                    # no generic KPM reader may fabricate a configuration.
                    self.assertEqual(outcomes["prepare"].outcome, GatewayOutcome.UNKNOWN)
                    self.assertEqual(outcomes["commit"].outcome, GatewayOutcome.REJECTED)
                    self.assertEqual(list(adapter.bindings()), [])
                else:
                    self.assertEqual(outcomes["prepare"].outcome, GatewayOutcome.ACKED)
                    self.assertEqual(outcomes["ready"].outcome, GatewayOutcome.ACKED)
                    self.assertEqual(outcomes["commit"].outcome, GatewayOutcome.ACKED)
                    # A policy was created and bound to the transaction.
                    self.assertEqual(list(adapter.bindings()), ["tx-1"])

    def test_plan_names_the_family_adapter_and_the_single_config_axis(self):
        for key in FAMILY_CASES:
            fam, _, plan, _ = _route(key, counter_present=True)
            with self.subTest(family=key):
                self.assertEqual(plan["adapter"], f"r1-{key}")
                self.assertEqual([s["axis"] for s in plan["steps"]], [fam.axis])

    def test_the_family_adapter_is_on_the_official_oran_dynamic_path(self):
        for key in FAMILY_CASES:
            _, adapter, _, _ = _route(key, counter_present=True)
            with self.subTest(family=key):
                self.assertIs(adapter.actuator_path, ActuatorPath.OFFICIAL_ORAN_DYNAMIC)
                # An A1 policy path cannot host a contract watchdog.
                self.assertFalse(adapter.hosts_watchdogs)

    def test_reconstructed_builder_sequence_is_accepted_by_the_producer(self):
        fam = family("cap")
        case = FAMILY_CASES["cap"]
        producer = Campaign5PolicyProducer()
        build = make_policy_builder(
            fam, validity_provider=fixed_validity(*VALIDITY)
        )

        first = build({
            "transactionId": "tx-1", "fencingToken": 0,
            "scope": case["scope"], "axis": fam.axis,
            "value": case["target"],
        }, last_revision=0)
        self.assertEqual(
            producer.put_policy(fam.policy_type_id, "pol-1", first).http_status,
            201,
        )

        second = build({
            "transactionId": "tx-1", "fencingToken": 1,
            "scope": case["scope"], "axis": fam.axis,
            "value": {"maxDlPrbs": 8},
        }, last_revision=1)
        self.assertEqual(
            producer.put_policy(fam.policy_type_id, "pol-1", second).http_status,
            200,
        )

        rebuilt = make_policy_builder(
            fam, validity_provider=fixed_validity(*VALIDITY)
        )
        third = rebuilt({
            "transactionId": "tx-1", "fencingToken": 2,
            "scope": case["scope"], "axis": fam.axis,
            "value": {"maxDlPrbs": 6},
        }, last_revision=2)
        self.assertEqual(
            producer.put_policy(fam.policy_type_id, "pol-1", third).http_status,
            200,
        )
        self.assertEqual(
            producer.get_policy(fam.policy_type_id, "pol-1")["trace"],
            {"traceId": "tx-1#cellId=cell-1/ueId=ue-1", "revision": 3, "fencingToken": 2},
        )

        with self.assertRaisesRegex(Campaign5BuilderError, "stale fencingToken"):
            rebuilt({
                "transactionId": "tx-1", "fencingToken": 1,
                "scope": case["scope"], "axis": fam.axis,
                "value": {"maxDlPrbs": 4},
            }, last_revision=3)


class OfficialRouteDegradesHonestly(unittest.TestCase):
    def test_absent_counter_cannot_establish_the_baseline_and_stages_nothing(self):
        for key in FAMILY_CASES:
            _, adapter, _, outcomes = _route(key, counter_present=False)
            with self.subTest(family=key):
                self.assertEqual(outcomes["prepare"].outcome, GatewayOutcome.UNKNOWN)
                self.assertEqual(outcomes["commit"].outcome, GatewayOutcome.REJECTED)
                self.assertEqual(list(adapter.bindings()), [])

    def test_a_lab_setup_adapter_cannot_be_registered_on_this_path(self):
        class LabSetup:
            actuator_path = ActuatorPath.LAB_SETUP_PREPARATION

            def dispatch(self, *, token, command):  # pragma: no cover
                raise AssertionError("never dispatched")

        with self.assertRaises(GatewayRefusal):
            TokenBoundWriteGateway(adapters={"r1-cap": LabSetup()},
                                   safe_state={"dlPrbCap": {"maxDlPrbs": 24}},
                                   clock=lambda: CLOCK)


class OfficialRouteDiscoveryGate(unittest.TestCase):
    def _prepare_with_detail_mutation(self, mutate):
        fam = family("cap")
        case = FAMILY_CASES["cap"]
        producer = Campaign5PolicyProducer()
        kpm = DictKpmConfigReader()

        class MutatingPort(ProducerPolicyPort):
            def get_policy_type(self, policy_type_id):
                detail = dict(super().get_policy_type(policy_type_id))
                mutate(detail)
                return detail

        port = MutatingPort(producer, fam, kpm)
        adapter = build_official_adapter(
            fam, policy_port=port, validity_provider=fixed_validity(*VALIDITY),
            kpm_reader=kpm, cadence_ms=10, deadline_ms=50,
        )
        gateway = build_official_gateway(
            fam, adapter, safe_state={fam.axis: case["baseline"]}, clock=lambda: CLOCK,
        )
        plan = official_plan(fam, scope=case["scope"], baseline=case["baseline"],
                             target=case["target"])
        result = gateway.prepare(
            token=permit()("PREPARE", config_hash(plan["baselineConfig"]), 0),
            plan=plan,
        )
        self.assertEqual(result.outcome, GatewayOutcome.UNKNOWN)
        self.assertEqual(list(adapter.bindings()), [])

    def test_runtime_route_refuses_a_schema_digest_disagreement(self):
        self._prepare_with_detail_mutation(
            lambda detail: detail["policySchema"].__setitem__("title", "tampered")
        )

    def test_runtime_route_refuses_a_bare_action_number_without_definition(self):
        self._prepare_with_detail_mutation(
            lambda detail: detail.pop("ranFunctionDefinition")
        )


if __name__ == "__main__":
    unittest.main()
