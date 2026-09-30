"""A1-P and worker hardware-free round-trip tests for slice actuation."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import unittest

from oran.contract.jcs import jcs_sha256
from oran.slice_actuator.a1 import (
    A1Conflict,
    A1NotFound,
    A1PolicyProducer,
    A1ValidationError,
    POLICY_TYPE_ID,
)
from oran.slice_actuator.measurement import (
    CoreSliceEvidence,
    CounterSample,
    MockMeasurementCollector,
)
from oran.slice_actuator.model import PrbRatios
from oran.slice_actuator.worker import (
    AmbiguousDeliveryError,
    FencingError,
    MockRcTransport,
    MockUeAnchorResolver,
    RollbackError,
    ScopeConflict,
    SliceActuationWorker,
    WorkerError,
)


def policy(*, revision: int = 1, token: int = 1, minimum: int = 20,
           maximum: int = 80, dedicated: int = 10, trace_id: str = "trial-001") -> dict:
    return {
        "scope": {
            "plmnId": {"mcc": "208", "mnc": "95"},
            "snssai": {"sst": 222, "sd": "00007B"},
        },
        "quota": {
            "minPrbPolicyRatio": minimum,
            "maxPrbPolicyRatio": maximum,
            "dedicatedPrbPolicyRatio": dedicated,
        },
        "validity": {
            "notBefore": "2026-08-25T00:00:00Z",
            "notAfter": "2026-08-26T00:00:00Z",
        },
        "trace": {
            "traceId": trace_id,
            "revision": revision,
            "fencingToken": token,
        },
    }


class A1ProducerLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.producer = A1PolicyProducer()

    def test_policy_type_discovery_publishes_schemas_and_jcs_digests(self) -> None:
        discovered = self.producer.get_policytypes()
        self.assertEqual(discovered, [POLICY_TYPE_ID])
        metadata = self.producer.get_policytype(POLICY_TYPE_ID)
        self.assertEqual(set(metadata), {"policySchema", "statusSchema"})
        self.assertEqual(self.producer.schema_digests(), {
            "policySchemaJcsSha256": jcs_sha256(metadata["policySchema"]),
            "statusSchemaJcsSha256": jcs_sha256(metadata["statusSchema"]),
        })

    def test_put_get_status_round_trip_preserves_type(self) -> None:
        created = self.producer.put_policy(POLICY_TYPE_ID, "slice-policy-1", policy())
        self.assertEqual(created.http_status, 201)
        self.assertEqual(self.producer.get_policy(POLICY_TYPE_ID, "slice-policy-1"), policy())
        status = self.producer.get_status(POLICY_TYPE_ID, "slice-policy-1")
        self.assertEqual(status["policyTypeId"], POLICY_TYPE_ID)
        self.assertEqual(status["policyState"], "ACTIVE")
        self.assertEqual(status["enforceStatus"], "PENDING")

    def test_a1_p_v2_routes_expose_discovery_put_status_and_delete(self) -> None:
        discovery = self.producer.handle("GET", "/A1-P/v2/policytypes")
        self.assertEqual(discovery.status, 200)
        self.assertEqual(discovery.body, [POLICY_TYPE_ID])
        policy_type = self.producer.handle(
            "GET", f"/A1-P/v2/policytypes/{POLICY_TYPE_ID}",
        )
        self.assertEqual(set(policy_type.body), {"policySchema", "statusSchema"})
        base = f"/A1-P/v2/policytypes/{POLICY_TYPE_ID}/policies/slice-policy-1"
        created = self.producer.handle("PUT", base, policy())
        self.assertEqual(created.status, 201)
        policies = self.producer.handle(
            "GET", f"/A1-P/v2/policytypes/{POLICY_TYPE_ID}/policies",
        )
        self.assertEqual(policies.body, ["slice-policy-1"])
        status = self.producer.handle("GET", base + "/status")
        self.assertEqual(status.status, 200)
        self.assertEqual(status.body["policyTypeId"], POLICY_TYPE_ID)
        refused = self.producer.handle("DELETE", base)
        self.assertEqual(refused.status, 409)
        self.assertIn("rollback worker", refused.body["error"])
        self.assertEqual(self.producer.get_policy(POLICY_TYPE_ID, "slice-policy-1"), policy())

    def test_identical_put_is_idempotent_but_key_collision_is_rejected(self) -> None:
        first = self.producer.put_policy(POLICY_TYPE_ID, "slice-policy-1", policy())
        second = self.producer.put_policy(POLICY_TYPE_ID, "slice-policy-1", copy.deepcopy(policy()))
        self.assertEqual((first.http_status, second.http_status), (201, 200))
        changed = policy(minimum=30)
        with self.assertRaisesRegex(A1Conflict, "policy id"):
            self.producer.put_policy(POLICY_TYPE_ID, "slice-policy-1", changed)

    def test_newer_revision_updates_the_same_policy_resource(self) -> None:
        self.producer.put_policy(POLICY_TYPE_ID, "slice-policy-1", policy())
        updated = policy(revision=2, token=2, minimum=30)
        result = self.producer.put_policy(POLICY_TYPE_ID, "slice-policy-1", updated)
        self.assertEqual(result.http_status, 200)
        self.assertEqual(self.producer.get_policy(POLICY_TYPE_ID, "slice-policy-1"), updated)
        self.assertEqual(
            self.producer.get_status(POLICY_TYPE_ID, "slice-policy-1")["enforceStatus"],
            "PENDING",
        )

    def test_one_policy_per_slice_scope_is_enforced(self) -> None:
        self.producer.put_policy(POLICY_TYPE_ID, "slice-policy-1", policy())
        with self.assertRaisesRegex(A1Conflict, "slice scope"):
            self.producer.put_policy(POLICY_TYPE_ID, "slice-policy-2", policy(trace_id="trial-002"))

    def test_schema_rejects_missing_snssai_and_ratio_reversal(self) -> None:
        missing = policy()
        del missing["scope"]["snssai"]
        with self.assertRaisesRegex(A1ValidationError, "snssai"):
            self.producer.put_policy(POLICY_TYPE_ID, "missing", missing)
        reversed_policy = policy(minimum=90, maximum=20)
        with self.assertRaisesRegex(A1ValidationError, "minPrbPolicyRatio"):
            self.producer.put_policy(POLICY_TYPE_ID, "reversed", reversed_policy)


class WorkerRoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = MockRcTransport()
        self.transport.seed("208-95/222/00007B", PrbRatios(10, 70, 5))
        self.collector = MockMeasurementCollector()
        self.now = datetime(2026, 8, 25, 0, 0, 30, tzinfo=timezone.utc)
        self.worker = SliceActuationWorker(
            self.transport,
            self.collector,
            anchor_resolver=MockUeAnchorResolver({
                "208-95/222/00007B": "imsi-208950000000032",
            }),
            clock=lambda: self.now,
        )

    def _publish_effect_sample(self, *, trace_id: str = "trial-001",
                               observed_at: str = "2026-08-25T00:01:00Z",
                               include_core: bool = True) -> None:
        scope = {"plmnId": {"mcc": "208", "mnc": "95"}, "snssai": {"sst": 222, "sd": "00007B"}}
        self.collector.publish(CounterSample(
            name="RRU.PrbTotDl", value=47, unit="percent", scope=scope,
            observed_at=observed_at, source="E2SM-KPM", trace_id=trace_id,
        ))
        if include_core:
            self.collector.publish_core(CoreSliceEvidence(
                scope=scope, observed_at=observed_at, source="5GC-session-record",
                trace_id=trace_id, session_ref="208950000000032:10",
            ))

    def test_end_to_end_apply_requires_readback_and_slice_counter(self) -> None:
        pending = self.worker.apply("slice-policy-1", policy())
        self.assertFalse(pending.enforced)
        self._publish_effect_sample()
        self.now = datetime(2026, 8, 25, 0, 2, tzinfo=timezone.utc)
        outcome = self.worker.apply("slice-policy-1", policy())
        self.assertTrue(outcome.enforced)
        self.assertEqual(outcome.policy_type_id, POLICY_TYPE_ID)
        self.assertTrue(outcome.delivery_acknowledged)
        self.assertTrue(outcome.readback_verified)
        self.assertTrue(outcome.measurement_correlated)
        self.assertTrue(outcome.core_evidence_correlated)
        self.assertEqual(len(self.transport.requests), 1)
        request = self.transport.requests[0]
        self.assertEqual(request["header"]["ricStyleType"], 2)
        self.assertEqual(request["header"]["ricControlActionId"], 6)

    def test_ack_alone_never_declares_success(self) -> None:
        outcome = self.worker.apply("slice-policy-1", policy())
        self.assertTrue(outcome.delivery_acknowledged)
        self.assertTrue(outcome.readback_verified)
        self.assertFalse(outcome.measurement_correlated)
        self.assertFalse(outcome.enforced)

    def test_counter_for_another_trace_cannot_prove_effect(self) -> None:
        self.worker.apply("slice-policy-1", policy())
        self._publish_effect_sample(trace_id="another-trial")
        self.now = datetime(2026, 8, 25, 0, 2, tzinfo=timezone.utc)
        outcome = self.worker.apply("slice-policy-1", policy())
        self.assertFalse(outcome.measurement_correlated)
        self.assertFalse(outcome.enforced)

    def test_pre_control_sample_and_missing_core_evidence_cannot_prove_effect(self) -> None:
        self._publish_effect_sample(observed_at="2026-08-25T00:00:00Z")
        outcome = self.worker.apply("slice-policy-1", policy())
        self.assertFalse(outcome.measurement_correlated)
        self._publish_effect_sample(observed_at="2026-08-25T00:01:00Z", include_core=False)
        self.now = datetime(2026, 8, 25, 0, 2, tzinfo=timezone.utc)
        outcome = self.worker.apply("slice-policy-1", policy())
        self.assertTrue(outcome.measurement_correlated)
        self.assertFalse(outcome.core_evidence_correlated)
        self.assertFalse(outcome.enforced)

    def test_idempotency_does_not_send_a_second_control_request(self) -> None:
        first = self.worker.apply("slice-policy-1", policy())
        second = self.worker.apply("slice-policy-1", copy.deepcopy(policy()))
        self.assertEqual(first, second)
        self.assertEqual(len(self.transport.requests), 1)

    def test_policy_validity_and_missing_header_anchor_fail_before_send(self) -> None:
        self.now = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)
        with self.assertRaisesRegex(WorkerError, "validity"):
            self.worker.apply("expired", policy())
        unresolved = SliceActuationWorker(
            self.transport, self.collector,
            anchor_resolver=MockUeAnchorResolver({}), clock=lambda: self.now,
        )
        self.now = datetime(2026, 8, 25, 0, 0, 30, tzinfo=timezone.utc)
        with self.assertRaisesRegex(WorkerError, "UE anchor"):
            unresolved.apply("missing-anchor", policy())
        self.assertEqual(self.transport.requests, [])

    def test_missing_restorable_baseline_fails_before_transport_send(self) -> None:
        transport = MockRcTransport()
        worker = SliceActuationWorker(
            transport, self.collector,
            anchor_resolver=MockUeAnchorResolver({
                "208-95/222/00007B": "imsi-208950000000032",
            }),
            clock=lambda: self.now,
        )
        with self.assertRaisesRegex(RollbackError, "restorable previous"):
            worker.apply("no-baseline", policy())
        self.assertEqual(transport.requests, [])

    def test_ambiguous_delivery_reserves_fence_and_rollback_snapshot(self) -> None:
        class ApplyThenTimeoutTransport(MockRcTransport):
            def __init__(self) -> None:
                super().__init__()
                self.timeout_once = True

            def send(self, request, *, scope_key, fencing_token, idempotency_key):
                receipt = super().send(
                    request, scope_key=scope_key, fencing_token=fencing_token,
                    idempotency_key=idempotency_key,
                )
                if self.timeout_once:
                    self.timeout_once = False
                    raise TimeoutError("response lost after E2 write")
                return receipt

        transport = ApplyThenTimeoutTransport()
        transport.seed("208-95/222/00007B", PrbRatios(10, 70, 5))
        worker = SliceActuationWorker(
            transport, self.collector,
            anchor_resolver=MockUeAnchorResolver({
                "208-95/222/00007B": "imsi-208950000000032",
            }),
            clock=lambda: self.now,
        )
        with self.assertRaisesRegex(AmbiguousDeliveryError, "ambiguous"):
            worker.apply("slice-policy-1", policy(minimum=30, maximum=90, dedicated=15))
        retry = worker.apply(
            "slice-policy-1", policy(minimum=30, maximum=90, dedicated=15),
        )
        self.assertFalse(retry.delivery_acknowledged)
        self.assertEqual(len(transport.requests), 1)
        restored = worker.rollback("slice-policy-1", fencing_token=2)
        self.assertEqual(restored, PrbRatios(10, 70, 5))
        self.assertEqual(len(transport.requests), 2)

    def test_stale_fencing_token_and_scope_conflict_are_rejected(self) -> None:
        self.worker.apply("slice-policy-1", policy(token=5))
        with self.assertRaises(FencingError):
            self.worker.apply("slice-policy-1", policy(token=4, revision=2, minimum=25))
        with self.assertRaises(ScopeConflict):
            self.worker.apply("slice-policy-2", policy(token=6, trace_id="trial-2"))
        with self.assertRaises(ScopeConflict):
            self.worker.apply("slice-policy-2", policy(token=5))

    def test_rollback_restores_the_exact_previous_ratios(self) -> None:
        original = policy(minimum=10, maximum=70, dedicated=5, token=1)
        self.worker.apply("slice-policy-1", original)
        changed = policy(minimum=30, maximum=90, dedicated=15, token=2, revision=2)
        self.worker.apply("slice-policy-1", changed)
        restored = self.worker.rollback("slice-policy-1", fencing_token=3)
        self.assertEqual(restored.minimum, 10)
        self.assertEqual(restored.maximum, 70)
        self.assertEqual(restored.dedicated, 5)
        self.assertEqual(self.transport.readback(self.worker.scope_key(policy())), restored)

    def test_non_ts_28_552_counter_and_cell_only_scope_are_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "TS 28.552"):
            self.collector.publish(CounterSample(
                name="invented.slice.metric", value=1, unit="count",
                scope={"cellId": "1"}, observed_at="2026-08-25T00:01:00Z", source="mock", trace_id="trial-001",
            ))
        with self.assertRaisesRegex(ValueError, "S-NSSAI"):
            self.collector.publish(CounterSample(
                name="DRB.UEThpDl", value=1, unit="kbit/s",
                scope={"cellId": "1"}, observed_at="2026-08-25T00:01:00Z", source="E2SM-KPM", trace_id="trial-001",
            ))

    def test_a1_policy_to_typed_worker_updates_effect_status(self) -> None:
        producer = A1PolicyProducer()
        producer.put_policy(POLICY_TYPE_ID, "slice-policy-1", policy())
        worker = SliceActuationWorker(
            self.transport, self.collector, producer=producer,
            anchor_resolver=MockUeAnchorResolver({
                "208-95/222/00007B": "imsi-208950000000032",
            }),
            clock=lambda: self.now,
        )
        worker.apply(
            "slice-policy-1", producer.get_policy(POLICY_TYPE_ID, "slice-policy-1"),
        )
        self._publish_effect_sample()
        self.now = datetime(2026, 8, 25, 0, 2, tzinfo=timezone.utc)
        outcome = worker.apply(
            "slice-policy-1", producer.get_policy(POLICY_TYPE_ID, "slice-policy-1"),
        )
        self.assertTrue(outcome.enforced)
        status = producer.get_status(POLICY_TYPE_ID, "slice-policy-1")
        self.assertEqual(status["enforceStatus"], "ENFORCED")
        self.assertTrue(status["deliveryAcknowledged"])
        self.assertTrue(status["readbackVerified"])
        self.assertTrue(status["measurementCorrelated"])
        self.assertTrue(status["coreEvidenceCorrelated"])

    def test_delete_lifecycle_rolls_back_before_releasing_the_a1_resource(self) -> None:
        producer = A1PolicyProducer()
        producer.put_policy(
            POLICY_TYPE_ID, "slice-policy-1",
            policy(minimum=10, maximum=70, dedicated=5),
        )
        worker = SliceActuationWorker(
            self.transport, self.collector, producer=producer,
            anchor_resolver=MockUeAnchorResolver({
                "208-95/222/00007B": "imsi-208950000000032",
            }),
            clock=lambda: self.now,
        )
        worker.apply(
            "slice-policy-1", producer.get_policy(POLICY_TYPE_ID, "slice-policy-1"),
        )
        changed = policy(
            revision=2, token=2, minimum=30, maximum=90, dedicated=15,
        )
        producer.put_policy(POLICY_TYPE_ID, "slice-policy-1", changed)
        worker.apply("slice-policy-1", changed)
        base = f"/A1-P/v2/policytypes/{POLICY_TYPE_ID}/policies/slice-policy-1"
        deleted = producer.handle("DELETE", base)
        self.assertEqual(deleted.status, 204)
        self.assertEqual(
            self.transport.readback(self.worker.scope_key(policy())),
            PrbRatios(10, 70, 5),
        )
        with self.assertRaises(A1NotFound):
            producer.get_policy(POLICY_TYPE_ID, "slice-policy-1")

    def test_bound_delete_of_noop_policy_needs_no_synthetic_rollback(self) -> None:
        producer = A1PolicyProducer()
        baseline_policy = policy(minimum=10, maximum=70, dedicated=5)
        producer.put_policy(POLICY_TYPE_ID, "slice-policy-1", baseline_policy)
        worker = SliceActuationWorker(
            self.transport, self.collector, producer=producer,
            anchor_resolver=MockUeAnchorResolver({
                "208-95/222/00007B": "imsi-208950000000032",
            }),
            clock=lambda: self.now,
        )
        worker.apply("slice-policy-1", baseline_policy)
        base = f"/A1-P/v2/policytypes/{POLICY_TYPE_ID}/policies/slice-policy-1"
        self.assertEqual(producer.handle("DELETE", base).status, 204)
        self.assertEqual(self.transport.readback(worker.scope_key(policy())), PrbRatios(10, 70, 5))

    def test_bound_delete_without_any_apply_is_refused_and_retains_policy(self) -> None:
        producer = A1PolicyProducer()
        producer.put_policy(POLICY_TYPE_ID, "slice-policy-1", policy())
        SliceActuationWorker(
            self.transport, self.collector, producer=producer,
            anchor_resolver=MockUeAnchorResolver({
                "208-95/222/00007B": "imsi-208950000000032",
            }),
            clock=lambda: self.now,
        )
        base = f"/A1-P/v2/policytypes/{POLICY_TYPE_ID}/policies/slice-policy-1"
        refused = producer.handle("DELETE", base)
        self.assertEqual(refused.status, 409)
        self.assertEqual(producer.get_policy(POLICY_TYPE_ID, "slice-policy-1"), policy())

    def test_delete_rollback_timeout_is_a_conflict_and_retains_policy(self) -> None:
        class RollbackTimeoutTransport(MockRcTransport):
            def __init__(self) -> None:
                super().__init__()
                self.fail_rollback = False

            def send(self, request, *, scope_key, fencing_token, idempotency_key):
                if self.fail_rollback:
                    raise TimeoutError("rollback response timeout")
                return super().send(
                    request, scope_key=scope_key, fencing_token=fencing_token,
                    idempotency_key=idempotency_key,
                )

        transport = RollbackTimeoutTransport()
        transport.seed("208-95/222/00007B", PrbRatios(10, 70, 5))
        producer = A1PolicyProducer()
        producer.put_policy(POLICY_TYPE_ID, "slice-policy-1", policy())
        worker = SliceActuationWorker(
            transport, self.collector, producer=producer,
            anchor_resolver=MockUeAnchorResolver({
                "208-95/222/00007B": "imsi-208950000000032",
            }),
            clock=lambda: self.now,
        )
        worker.apply("slice-policy-1", policy())
        transport.fail_rollback = True
        base = f"/A1-P/v2/policytypes/{POLICY_TYPE_ID}/policies/slice-policy-1"
        refused = producer.handle("DELETE", base)
        self.assertEqual(refused.status, 409)
        self.assertIn("rollback response timeout", refused.body["error"])
        self.assertEqual(producer.get_policy(POLICY_TYPE_ID, "slice-policy-1"), policy())

    def test_delete_after_multiple_updates_restores_pre_policy_baseline(self) -> None:
        producer = A1PolicyProducer()
        first = policy(minimum=20, maximum=80, dedicated=10)
        producer.put_policy(POLICY_TYPE_ID, "slice-policy-1", first)
        worker = SliceActuationWorker(
            self.transport, self.collector, producer=producer,
            anchor_resolver=MockUeAnchorResolver({
                "208-95/222/00007B": "imsi-208950000000032",
            }),
            clock=lambda: self.now,
        )
        worker.apply("slice-policy-1", first)
        second = policy(revision=2, token=2, minimum=30, maximum=90, dedicated=15)
        producer.put_policy(POLICY_TYPE_ID, "slice-policy-1", second)
        worker.apply("slice-policy-1", second)
        base = f"/A1-P/v2/policytypes/{POLICY_TYPE_ID}/policies/slice-policy-1"
        self.assertEqual(producer.handle("DELETE", base).status, 204)
        self.assertEqual(self.transport.readback(worker.scope_key(policy())), PrbRatios(10, 70, 5))


if __name__ == "__main__":
    unittest.main()
