from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote
from unittest.mock import patch

from oran.nonrt.a1_client import A1PClient, TransportResponse
from oran.nonrt._contract import jcs_sha256
from oran.nonrt.capability import (
    CapabilityArtifacts,
    CapabilityError,
    load_capability_manifest,
    validate_e2_inventory,
)
from oran.nonrt.config import SecurityProfile
from oran.nonrt.__main__ import create_service_from_values
from oran.nonrt.service import NonRtRicService, ResponseDropped, SimulatedCrash


def contract_bundle() -> Path:
    configured = os.environ.get("ORAN_CONTRACT_BUNDLE")
    candidates = [Path(configured)] if configured else []
    worktree_root = Path(__file__).resolve().parents[3]
    candidates.append(worktree_root / "contracts/oran-aic/1.0.0/shared-contract-bundle")
    for candidate in candidates:
        if (candidate / "bundle-manifest.1.0.0.json").is_file():
            return candidate
    raise RuntimeError(
        "shared contract bundle not found; set ORAN_CONTRACT_BUNDLE to the pinned bundle"
    )


def contract_policy_type_object() -> dict:
    """The ``PolicyTypeObject`` a conformant A1-P v2 producer returns.

    Exactly the two members §7.1 of the frozen mandatory contract defines, whose
    bytes are the frozen bundle's own schemas.  The fake transport below is
    deliberately *not* richer than that: a fake that carried an extra member is
    what let ``assert_a1_discovery`` require one no producer sends.
    """
    bundle = contract_bundle()
    return {
        "policySchema": json.loads(
            (bundle / "AIC_UECellSteering_1.0.0.policy.schema.json")
            .read_text(encoding="utf-8")),
        "statusSchema": json.loads(
            (bundle / "AIC_UECellSteering_1.0.0.status.schema.json")
            .read_text(encoding="utf-8")),
    }


class FakeA1Transport:
    def __init__(self):
        self.policies: dict[str, dict] = {}
        self.statuses: dict[str, dict] = {}
        self.put_count = 0
        self.delete_count = 0
        self.fail_next_put = False
        self.requests: list[tuple[str, str, object, object]] = []

    def request(self, method, path, *, query=None, body=None):
        self.requests.append((method, path, query, body))
        parts = path.split("/")
        if method == "GET" and path == "/A1-P/v2/policytypes":
            return TransportResponse(200, ["AIC_UECellSteering_1.0.0"])
        if method == "GET" and path.endswith("/AIC_UECellSteering_1.0.0"):
            return TransportResponse(200, contract_policy_type_object())
        if method == "GET" and path.endswith("/policies"):
            return TransportResponse(200, sorted(self.policies))
        if path.endswith("/status"):
            policy_id = unquote(parts[-2])
            if method == "GET" and policy_id in self.statuses:
                return TransportResponse(200, copy.deepcopy(self.statuses[policy_id]))
            return TransportResponse(404)
        policy_id = unquote(parts[-1])
        if method == "PUT":
            if self.fail_next_put:
                self.fail_next_put = False
                raise OSError("ambiguous A1 transport failure")
            created = policy_id not in self.policies
            self.policies[policy_id] = copy.deepcopy(body)
            self.put_count += 1
            return TransportResponse(201 if created else 200, copy.deepcopy(body))
        if method == "GET":
            if policy_id not in self.policies:
                return TransportResponse(404)
            return TransportResponse(200, copy.deepcopy(self.policies[policy_id]))
        if method == "DELETE":
            if policy_id not in self.policies:
                return TransportResponse(404)
            del self.policies[policy_id]
            self.delete_count += 1
            return TransportResponse(204)
        raise AssertionError((method, path))


class NonRtFrameworkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = contract_bundle()
        golden = json.loads((cls.bundle / "golden/golden-vectors.1.0.0.json").read_text())
        cls.policy = golden["canonicalObjects"]["policy"]
        cls.capability = golden["canonicalObjects"]["capabilityManifest"]
        cls.status = golden["canonicalObjects"]["appliedVerifiedStatus"]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.transport = FakeA1Transport()
        self.callbacks: list[tuple[str, dict]] = []
        self.service = self.make_service()

    def tearDown(self):
        self.temp.cleanup()

    def make_service(self):
        return NonRtRicService(
            database_path=Path(self.temp.name) / "nonrt.sqlite3",
            a1_client=A1PClient(self.transport),
            capability_manifest=self.capability,
            a1_ready=True,
            bundle_dir=self.bundle,
            security=SecurityProfile(insecure_dev_mode=True, listen_host="127.0.0.1"),
            r1_api_root="http://127.0.0.1:18080",
            a1_notification_destination="http://127.0.0.1:18080/callbacks/a1-status",
            bootstrap_info={"bootstrapInformation": []},
            callback_sender=self._callback,
        )

    def _callback(self, uri, body):
        self.callbacks.append((uri, copy.deepcopy(body)))
        return 204

    def policy_information(self, policy=None):
        return {
            "nearRtRicId": self.capability["nearRtRicId"],
            "policyTypeId": "AIC_UECellSteering_1.0.0",
            "policyObject": copy.deepcopy(policy or self.policy),
        }

    @staticmethod
    def headers():
        return {"Version": "1.0.0", "X-Authenticated-RApp-Id": "rapp-test"}

    def create(self, policy=None):
        return self.service.handle(
            "POST",
            "/a1-policy-management/v1/policies",
            headers=self.headers(),
            body=self.policy_information(policy),
        )

    def test_response_loss_reuses_first_201_and_performs_no_new_a1_put(self):
        reserve = self.service.handle(
            "POST",
            "/harness/fault",
            body={
                "fault": "DROP_HTTP_RESPONSE",
                "boundary": {"stage": "AFTER_R1_CREATE_COMMIT"},
            },
        )
        self.assertEqual(200, reserve.status)
        with self.assertRaises(ResponseDropped):
            self.create()
        replay = self.create()
        self.assertEqual(201, replay.status)
        self.assertEqual(1, self.transport.put_count)
        self.assertEqual(replay.headers["Location"].split("/")[-1], next(iter(self.transport.policies)))

    def test_response_loss_restores_harness_assigned_policy_id_from_ledger(self):
        assigned_policy_id = "42c56544-f2d1-40a5-ab1d-3de8c5ee92dc"
        configured = self.service.handle(
            "POST",
            "/harness/op",
            body={
                "op": "CONFIGURE_NEXT_POLICY_ID",
                "policyId": assigned_policy_id,
            },
        )
        self.assertEqual(200, configured.status)
        reserve = self.service.handle(
            "POST",
            "/harness/fault",
            body={
                "fault": "DROP_HTTP_RESPONSE",
                "boundary": {"stage": "AFTER_R1_CREATE_COMMIT"},
            },
        )
        self.assertEqual(200, reserve.status)

        with self.assertRaises(ResponseDropped):
            self.create()

        replay = self.create()
        self.assertEqual(201, replay.status)
        self.assertEqual(assigned_policy_id, replay.headers["Location"].split("/")[-1])
        self.assertEqual([assigned_policy_id], list(self.transport.policies))
        self.assertEqual(1, self.transport.put_count)

    def test_crash_after_wal_before_a1_put_reconciles_from_durable_state(self):
        self.service.handle(
            "POST",
            "/harness/fault",
            body={
                "fault": "CRASH_PROCESS",
                "boundary": {"stage": "AFTER_LEDGER_BEFORE_A1_PUT"},
            },
        )
        with self.assertRaises(SimulatedCrash):
            self.create()
        self.assertEqual(0, self.transport.put_count)
        pending = self.service.handle("GET", "/harness/state")
        self.assertEqual("WAL_PENDING", pending.body["policies"][0]["state"])
        # Exercise the public restart control plane.  Reconstructing a test
        # service here would hide a broken /harness/restart implementation.
        restarted = self.service.handle(
            "POST",
            "/harness/restart",
            body={"component": "NON_RT_RIC_FRAMEWORK"},
        )
        self.assertEqual(200, restarted.status)
        self.assertTrue(restarted.body["outputs"]["restartCompleted"])
        recovered = self.service.handle("GET", "/harness/state")
        self.assertEqual("WAL_PENDING", recovered.body["policies"][0]["state"])
        policy_id = recovered.body["policies"][0]["policy_id"]
        self.transport.request(
            "PUT",
            f"/A1-P/v2/policytypes/AIC_UECellSteering_1.0.0/policies/{policy_id}",
            body=copy.deepcopy(self.policy),
        )
        replay = self.create()
        self.assertEqual(201, replay.status)
        self.assertEqual(1, self.transport.put_count)
        list_requests = [request for request in self.transport.requests if request[0:2] == (
            "GET", "/A1-P/v2/policytypes/AIC_UECellSteering_1.0.0/policies"
        )]
        self.assertTrue(list_requests, "reconciliation must query before retrying PUT")

    def test_first_create_ambiguous_put_becomes_uncertain_until_requery_and_retry(self):
        self.transport.fail_next_put = True
        failed = self.create()
        self.assertEqual(503, failed.status)
        state = self.service.handle("GET", "/harness/state")
        self.assertEqual("UNCERTAIN", state.body["policies"][0]["state"])
        self.assertEqual(0, self.transport.put_count)

        # A client first re-queries the R1 collection and compares the durable
        # trace key before safely replaying its idempotent create.
        listed = self.service.handle(
            "GET",
            "/a1-policy-management/v1/policies?nearRtRicId="
            f"{self.capability['nearRtRicId']}&policyTypeId=AIC_UECellSteering_1.0.0",
            headers=self.headers(),
        )
        self.assertEqual(200, listed.status)
        self.assertEqual(
            self.policy["trace"]["idempotencyKey"],
            listed.body[0]["policyObject"]["trace"]["idempotencyKey"],
        )
        replay = self.create()
        self.assertEqual(201, replay.status)
        self.assertEqual(1, self.transport.put_count)
        self.assertEqual("ACTIVE", self.service.handle("GET", "/harness/state").body["policies"][0]["state"])

    def test_same_first_create_key_with_different_payload_is_409_zero_write(self):
        self.assertEqual(201, self.create().status)
        changed = copy.deepcopy(self.policy)
        changed["trace"]["policyRevision"] += 1
        conflict = self.create(changed)
        self.assertEqual(409, conflict.status)
        self.assertEqual("AIC_IDEMPOTENCY_CONFLICT", conflict.body["title"])
        self.assertEqual(1, self.transport.put_count)

    def test_near_rt_restart_replays_durable_desired_policy(self):
        created = self.create()
        policy_id = created.headers["Location"].split("/")[-1]
        self.transport.policies.clear()
        restarted = self.service.handle(
            "POST",
            "/harness/restart",
            body={"component": "NON_RT_RIC_FRAMEWORK"},
        )
        self.assertEqual({"outputs": {"restartCompleted": True}}, restarted.body)
        self.service.reconcile_desired_state()
        self.assertIn(policy_id, self.transport.policies)
        self.assertEqual(2, self.transport.put_count)

    def test_pending_update_survives_ambiguous_failure_and_restart(self):
        created = self.create()
        policy_id = created.headers["Location"].split("/")[-1]
        update = copy.deepcopy(self.policy)
        update["trace"]["intentRevision"] += 1
        update["trace"]["policyRevision"] += 1
        update["trace"]["idempotencyKey"] += ":update"
        self.transport.fail_next_put = True
        failed = self.service.handle(
            "PUT",
            f"/a1-policy-management/v1/policies/{policy_id}",
            headers=self.headers(),
            body=update,
        )
        self.assertEqual(503, failed.status)
        recovered = self.service.handle(
            "POST",
            "/harness/restart",
            body={"component": "NON_RT_RIC_FRAMEWORK"},
        )
        self.assertEqual(200, recovered.status)
        self.service.reconcile_desired_state()
        self.assertEqual(update, self.transport.policies[policy_id])

    def test_status_new_epoch_queries_then_relays_r1_wrapper(self):
        created = self.create()
        policy_id = created.headers["Location"].split("/")[-1]
        subscription = self.service.handle(
            "POST",
            "/a1-policy-management/v1/policies/subscriptions",
            headers=self.headers(),
            body={
                "notificationDestination": "http://127.0.0.1:19090/status",
                "policyIdList": [policy_id],
            },
        )
        status = copy.deepcopy(self.status)
        status["aicStatus"]["policyId"] = policy_id
        self.transport.statuses[policy_id] = status
        result = self.service.receive_a1_status(policy_id, status)
        self.assertEqual(204, result.status)
        self.assertEqual(201, subscription.status)
        wrapper = self.callbacks[-1][1]
        self.assertEqual(subscription.body["subscriptionId"], wrapper["subscriptionId"])
        self.assertEqual(policy_id, wrapper["policyStates"][0]["policyId"])
        self.assertIn("policyStatusObject", wrapper["policyStates"][0])

    def test_status_duplicate_and_late_sequences_are_audited_without_relay(self):
        policy_id = self.create().headers["Location"].split("/")[-1]
        subscription = self.service.handle(
            "POST",
            "/a1-policy-management/v1/policies/subscriptions",
            headers=self.headers(),
            body={"notificationDestination": "http://127.0.0.1:19090/status", "policyIdList": [policy_id]},
        )
        self.assertEqual(201, subscription.status)
        status = copy.deepcopy(self.status)
        status["aicStatus"]["policyId"] = policy_id
        self.transport.statuses[policy_id] = status
        self.service.receive_a1_status(policy_id, status)
        self.assertEqual(1, len(self.callbacks))

        self.service.receive_a1_status(policy_id, copy.deepcopy(status))
        late = copy.deepcopy(status)
        late["aicStatus"]["statusSeq"] -= 1
        self.service.receive_a1_status(policy_id, late)
        self.assertEqual(1, len(self.callbacks), "duplicate and late status must not relay")
        audit = self.service.store.rows(
            "SELECT disposition FROM status_audit WHERE policy_id=? ORDER BY audit_id", (policy_id,)
        )
        self.assertEqual(
            ["NEW_EPOCH_NOTIFICATION_QUERY_REQUIRED", "APPLIED_QUERY", "DUPLICATE", "LATE_LOWER_SEQUENCE"],
            [row["disposition"] for row in audit],
        )
        snapshot = self.service.store.snapshot()
        self.assertEqual(status["aicStatus"]["producerEpoch"], snapshot["finalProducerEpoch"])
        self.assertEqual(status["aicStatus"]["statusSeq"], snapshot["finalStatusSeq"])
        self.assertEqual(1, snapshot["ignoredLowerStatusSeqCount"])
        self.assertEqual(0, snapshot["statusRegressionCount"])

    def test_concurrent_status_callbacks_cannot_regress_sequence(self):
        policy_id = self.create().headers["Location"].split("/")[-1]
        initial = copy.deepcopy(self.status)
        initial["aicStatus"].update({"policyId": policy_id, "statusSeq": 7})
        self.transport.statuses[policy_id] = initial
        self.service.receive_a1_status(policy_id, initial)

        barrier = threading.Barrier(3)
        errors = []

        def deliver(sequence):
            candidate = copy.deepcopy(initial)
            candidate["aicStatus"]["statusSeq"] = sequence
            try:
                barrier.wait()
                self.service.receive_a1_status(policy_id, candidate)
            except Exception as exc:  # pragma: no cover - assertion reports worker errors
                errors.append(exc)

        workers = [threading.Thread(target=deliver, args=(sequence,))
                   for sequence in (8, 9)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join()

        self.assertEqual([], errors)
        stored = self.service.store.row(
            "SELECT status_seq FROM statuses WHERE policy_id=?", (policy_id,))
        self.assertEqual(9, stored["status_seq"])
        self.assertEqual(0, self.service.store.snapshot()["statusRegressionCount"])

    def test_status_lock_is_released_before_a1_query_and_r1_callback(self):
        policy_id = self.create().headers["Location"].split("/")[-1]
        self.service.handle(
            "POST", "/a1-policy-management/v1/policies/subscriptions",
            headers=self.headers(),
            body={"notificationDestination": "http://127.0.0.1:19090/status",
                  "policyIdList": [policy_id]},
        )
        status = copy.deepcopy(self.status)
        status["aicStatus"]["policyId"] = policy_id
        self.transport.statuses[policy_id] = status
        ownership: list[tuple[str, bool]] = []
        original_request = self.transport.request

        def observed_request(method, path, **kwargs):
            if method == "GET" and path.endswith("/status"):
                ownership.append(("a1", self.service._status_lock._is_owned()))
            return original_request(method, path, **kwargs)

        def observed_callback(uri, body):
            ownership.append(("r1", self.service._status_lock._is_owned()))
            return self._callback(uri, body)

        self.transport.request = observed_request
        self.service.callback_sender = observed_callback
        self.service.receive_a1_status(policy_id, status)
        self.assertEqual([("a1", False), ("r1", False)], ownership)

    def test_drop_callback_delivery_is_visible_in_harness_state_and_audit(self):
        policy_id = self.create().headers["Location"].split("/")[-1]
        subscription = self.service.handle(
            "POST",
            "/a1-policy-management/v1/policies/subscriptions",
            headers=self.headers(),
            body={"notificationDestination": "http://127.0.0.1:19090/status", "policyIdList": [policy_id]},
        )
        self.assertEqual(201, subscription.status)
        fault = self.service.handle(
            "POST",
            "/harness/fault",
            body={
                "fault": "DROP_CALLBACK_DELIVERY",
                "boundary": {"sender": "NON_RT_RIC_FRAMEWORK", "interface": "R1_STATUS"},
            },
        )
        self.assertEqual(200, fault.status)
        status = copy.deepcopy(self.status)
        status["aicStatus"]["policyId"] = policy_id
        self.transport.statuses[policy_id] = status
        self.service.receive_a1_status(policy_id, status)
        self.assertEqual([], self.callbacks)
        harness_state = self.service.handle("GET", "/harness/state")
        self.assertEqual(policy_id, harness_state.body["policyStatuses"][0]["policy_id"])
        audit = self.service.store.rows(
            "SELECT disposition FROM status_audit WHERE policy_id=? ORDER BY audit_id", (policy_id,)
        )
        self.assertIn("R1_CALLBACK_DROPPED", [row["disposition"] for row in audit])

    def test_callback_transport_failure_does_not_reject_durable_a1_ingress(self):
        policy_id = self.create().headers["Location"].split("/")[-1]
        self.service.handle(
            "POST", "/a1-policy-management/v1/policies/subscriptions",
            headers=self.headers(),
            body={"notificationDestination": "http://127.0.0.1:19090/status",
                  "policyIdList": [policy_id]},
        )
        self.service.callback_sender = lambda _uri, _body: (_ for _ in ()).throw(
            OSError("callback unavailable"))
        status = copy.deepcopy(self.status)
        status["aicStatus"]["policyId"] = policy_id
        self.transport.statuses[policy_id] = status

        self.assertEqual(204, self.service.receive_a1_status(policy_id, status).status)
        audit = self.service.store.rows(
            "SELECT disposition FROM status_audit WHERE policy_id=? ORDER BY audit_id",
            (policy_id,),
        )
        self.assertIn("R1_CALLBACK_TRANSPORT_FAILED", [row["disposition"] for row in audit])

    def test_retired_producer_epoch_cannot_replace_newer_epoch(self):
        policy_id = self.create().headers["Location"].split("/")[-1]
        first = copy.deepcopy(self.status)
        first["aicStatus"].update({
            "policyId": policy_id,
            "producerEpoch": "11111111-1111-4111-8111-111111111111",
            "statusSeq": 1,
        })
        self.transport.statuses[policy_id] = first
        self.service.receive_a1_status(policy_id, first)

        current = copy.deepcopy(first)
        current["aicStatus"].update({
            "producerEpoch": "22222222-2222-4222-8222-222222222222",
            "statusSeq": 1,
        })
        self.transport.statuses[policy_id] = current
        self.service.receive_a1_status(policy_id, current)

        replay = copy.deepcopy(first)
        replay["aicStatus"]["statusSeq"] = 99
        self.transport.statuses[policy_id] = replay
        self.service.receive_a1_status(policy_id, replay)
        self.service._consume_status(policy_id, replay, queried=True)

        stored = self.service.store.row(
            "SELECT producer_epoch,status_seq FROM statuses WHERE policy_id=?", (policy_id,))
        self.assertEqual(current["aicStatus"]["producerEpoch"], stored["producer_epoch"])
        self.assertEqual(1, stored["status_seq"])
        audit = self.service.store.rows(
            "SELECT disposition FROM status_audit WHERE policy_id=? ORDER BY audit_id",
            (policy_id,),
        )
        self.assertEqual("OLD_PRODUCER_EPOCH_QUERY", audit[-1]["disposition"])
        self.assertEqual(1, self.service.store.snapshot()["ignoredOldProducerEpochCount"])

    def test_harness_policy_seed_mirrors_initial_status_epoch_durably(self):
        policy_id = self.status["aicStatus"]["policyId"]
        seeded = self.service.handle(
            "POST", "/harness/op",
            body={
                "op": "INSTALL_NON_RT_DESIRED_POLICY",
                "policyId": policy_id,
                "policy": copy.deepcopy(self.policy),
                "status": copy.deepcopy(self.status),
            },
        )
        self.assertEqual(200, seeded.status)
        snapshot = self.service.store.snapshot()
        self.assertEqual(self.status["aicStatus"]["producerEpoch"],
                         snapshot["finalProducerEpoch"])
        audit = self.service.store.rows(
            "SELECT disposition FROM status_audit WHERE policy_id=?", (policy_id,))
        self.assertEqual(["APPLIED_INITIAL_STATE"], [row["disposition"] for row in audit])

    def test_pinned_uri_and_dme_outer_shape(self):
        typo = self.service.handle(
            "GET",
            "/a1-policy-managment/v1/policy-types",
            headers=self.headers(),
        )
        self.assertEqual(404, typo.status)
        missing_invoker = self.service.handle(
            "GET", "/service-apis/v1/allServiceAPIs", headers={"Version": "1.2.0"}
        )
        self.assertEqual(400, missing_invoker.status)
        invalid = self.service.handle(
            "POST",
            "/data-registration/v2/production-capabilities",
            headers={"Version": "2.0.0-alpha.2"},
            body={"dmeTypeDefinition": {}, "dataAccessEndpoint": "x", "dataDeliveryMode": []},
        )
        self.assertEqual(400, invalid.status)

    def test_service_registration_discovery_and_version_signaling(self):
        description = {
            "apiName": "rapp-callback",
            "apiVersion": "v1",
            "aefProfiles": [{"aefId": "aef-test"}],
            "communicationType": "REQUEST_RESPONSE",
            "vendorSpecific-o-ran.org": {"fullApiVersions": ["1.0.0"]},
        }
        registered = self.service.handle(
            "POST",
            "/published-apis/v1/rapp-test/service-apis",
            headers={"Version": "1.2.0"},
            body=description,
        )
        self.assertEqual(201, registered.status)
        self.assertEqual("1.2.0", registered.headers["Version"])
        self.assertEqual(registered.body["apiId"], registered.headers["Location"].split("/")[-1])
        discovered = self.service.handle(
            "GET",
            "/service-apis/v1/allServiceAPIs?api-invoker-id=rapp-test&api-version=v1",
            headers={"Version": "1.2.0"},
        )
        self.assertEqual([registered.body], discovered.body)
        wrong_case = self.service.handle(
            "GET",
            "/service-apis/v1/allserviceapis?api-invoker-id=rapp-test",
            headers={"Version": "1.2.0"},
        )
        self.assertEqual(404, wrong_case.status)

    def test_harness_installs_all_pinned_service_discovery_profiles(self):
        versions = {
            "service-registration": "1.2.0",
            "service-discovery": "1.2.0",
            "a1-policy-management": "1.0.0",
            "data-access": "2.0.0-alpha.2",
        }
        installed = self.service.handle(
            "POST", "/harness/op",
            body={
                "op": "INSTALL_PINNED_SERVICE_DESCRIPTIONS",
                "rAppId": "rapp-test",
                "discoveredVersions": versions,
            },
        )
        self.assertEqual(200, installed.status)
        discovered = self.service.handle(
            "GET", "/service-apis/v1/allServiceAPIs?api-invoker-id=rapp-test",
            headers={"Version": "1.2.0"},
        )
        self.assertEqual(versions, {
            item["apiName"]: item["vendorSpecific-o-ran.org"]["fullApiVersions"][0]
            for item in discovered.body
        })

    def test_dme_registration_percent_encoded_discovery_and_job_crud(self):
        filter_schema = json.loads(
            (self.bundle / "aic.policy-evidence-filter.1.0.0.schema.json").read_text()
        )
        evidence_schema = json.loads(
            (self.bundle / "aic.policy-evidence.1.0.0.schema.json").read_text()
        )
        registration = {
            "dmeTypeDefinition": {
                "dmeTypeId": {"namespace": "aic", "name": "policy-evidence", "version": "1.0.0"},
                "metadata": {"dataCategory": ["PERFORMANCE"]},
                "dataProductionSchema": filter_schema,
                "dataDeliverySchemas": [
                    {
                        "type": "JSON_SCHEMA",
                        "deliverySchemaId": "aic.policy-evidence.record.schema.1.0.0",
                        "schema": json.dumps(evidence_schema, sort_keys=True, separators=(",", ":")),
                    }
                ],
                "dataDeliveryMechanisms": [{"dataDeliveryMethod": "PUSH_HTTP"}],
            },
            "dataAccessEndpoint": "http://127.0.0.1:18080/data-access/v2",
            "dataDeliveryModes": ["CONTINUOUS"],
        }
        registered = self.service.handle(
            "POST",
            "/data-registration/v2/production-capabilities",
            headers={"Version": "2.0.0-alpha.2"},
            body=registration,
        )
        self.assertEqual(201, registered.status)
        discovered = self.service.handle(
            "GET",
            "/data-discovery/v2/dme-types/aic%3Apolicy-evidence%3A1.0.0",
            headers={"Version": "2.0.0"},
        )
        self.assertEqual([registration], discovered.body)
        policy_id = self.create().headers["Location"].split("/")[-1]
        job = {
            "dataDeliveryMode": "CONTINUOUS",
            "dmeTypeId": "aic:policy-evidence:1.0.0",
            "productionJobDefinition": {
                "policyTypeId": "AIC_UECellSteering_1.0.0",
                "policyId": policy_id,
                "minimumPolicyRevision": 1,
                "nearRtRicId": self.capability["nearRtRicId"],
            },
            "dataDeliveryMethod": "PUSH_HTTP",
            "dataDeliverySchemaId": "aic.policy-evidence.record.schema.1.0.0",
            "pushDeliveryDetailsHttp": {
                "dataPushUri": "http://127.0.0.1:19090/evidence/0123456789abcdef0123456789abcdef"
            },
        }
        created = self.service.handle(
            "POST",
            "/data-access/v2/data-jobs",
            headers={"Version": "2.0.0-alpha.2"},
            body=job,
        )
        self.assertEqual(201, created.status)
        self.assertEqual(set(job) | {"dataJobInfoStatus"}, set(created.body))
        job_id = created.headers["Location"].split("/")[-1]
        status = self.service.handle(
            "GET",
            f"/data-access/v2/data-jobs/{job_id}/status",
            headers={"Version": "2.0.0-alpha.2"},
        )
        self.assertEqual({"dataJobInfoStatus": "RUNNING", "acceptedPushPayloadCount": 0},
                         status.body)
        deleted_policy = self.service.handle(
            "DELETE",
            f"/a1-policy-management/v1/policies/{policy_id}",
            headers=self.headers(),
        )
        self.assertEqual(204, deleted_policy.status)
        absent_job = self.service.handle(
            "GET",
            f"/data-access/v2/data-jobs/{job_id}",
            headers={"Version": "2.0.0-alpha.2"},
        )
        self.assertEqual(404, absent_job.status)

    def test_dme_job_binding_does_not_require_local_policy_replica(self):
        filter_schema = json.loads(
            (self.bundle / "aic.policy-evidence-filter.1.0.0.schema.json").read_text())
        evidence_schema = json.loads(
            (self.bundle / "aic.policy-evidence.1.0.0.schema.json").read_text())
        registration = {
            "dmeTypeDefinition": {
                "dmeTypeId": {"namespace": "aic", "name": "policy-evidence", "version": "1.0.0"},
                "metadata": {"dataCategory": ["PERFORMANCE"]},
                "dataProductionSchema": filter_schema,
                "dataDeliverySchemas": [{
                    "type": "JSON_SCHEMA",
                    "deliverySchemaId": "aic.policy-evidence.record.schema.1.0.0",
                    "schema": json.dumps(evidence_schema, sort_keys=True, separators=(",", ":")),
                }],
                "dataDeliveryMechanisms": [{"dataDeliveryMethod": "PUSH_HTTP"}],
            },
            "dataAccessEndpoint": "http://127.0.0.1:18080/data-access/v2",
            "dataDeliveryModes": ["CONTINUOUS"],
        }
        self.assertEqual(201, self.service.handle(
            "POST", "/data-registration/v2/production-capabilities",
            headers={"Version": "2.0.0-alpha.2"}, body=registration).status)
        job = {
            "dataDeliveryMode": "CONTINUOUS",
            "dmeTypeId": "aic:policy-evidence:1.0.0",
            "productionJobDefinition": {
                "policyTypeId": "AIC_UECellSteering_1.0.0",
                "policyId": "externally-assigned-policy-id",
                "minimumPolicyRevision": 1,
                "nearRtRicId": self.capability["nearRtRicId"],
            },
            "dataDeliveryMethod": "PUSH_HTTP",
            "dataDeliverySchemaId": "aic.policy-evidence.record.schema.1.0.0",
            "pushDeliveryDetailsHttp": {
                "dataPushUri": "http://127.0.0.1:19090/evidence/0123456789abcdef0123456789abcdef"},
        }
        created = self.service.handle(
            "POST", "/data-access/v2/data-jobs",
            headers={"Version": "2.0.0-alpha.2"}, body=job)
        self.assertEqual(201, created.status)
        record = json.loads(
            (self.bundle / "golden/golden-vectors.1.0.0.json").read_text()
        )["canonicalObjects"]["afterEvidence"]
        self.service.record_dme_push(
            "0123456789abcdef0123456789abcdef", record)
        self.assertEqual(
            [jcs_sha256(record)],
            self.service.store.snapshot()["acceptedPushPayloadDigests"],
        )

    def test_service_factory_uses_only_integration_values_for_endpoints(self):
        artifacts = SimpleNamespace(capability_manifest=self.capability, a1_ready=True)
        values = {
            "r1.apiRoot": "https://r1.values.example/v1",
            "a1.apiRoot": "https://a1.values.example/v2",
            "a1.notificationDestination": "https://a1.values.example/callbacks/nonrt",
        }
        with patch("oran.nonrt.__main__.CapabilityArtifacts.load", return_value=artifacts) as loader:
            service = create_service_from_values(
                values,
                bundle_dir=self.bundle,
                database_path=Path(self.temp.name) / "configured.sqlite3",
                artifact_base_dir=self.temp.name,
                listen_host="127.0.0.1",
                insecure_dev_mode=True,
                a1_client=A1PClient(self.transport),
                callback_sender=self._callback,
            )
        self.assertEqual("https://r1.values.example/v1", service.r1_api_root)
        self.assertEqual("https://a1.values.example/callbacks/nonrt", service.a1_notification_destination)
        self.assertEqual("/callbacks/nonrt", service.a1_notification_path)
        self.assertEqual(values, loader.call_args.args[0])


class CapabilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = contract_bundle()
        golden = json.loads((cls.bundle / "golden/golden-vectors.1.0.0.json").read_text())
        cls.capability = golden["canonicalObjects"]["capabilityManifest"]

    def test_golden_capability_accepts_and_byte_tamper_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capability.json"
            encoded = json.dumps(self.capability, separators=(",", ":")).encode()
            path.write_bytes(encoded)
            digest = hashlib.sha256(encoded).hexdigest()
            accepted = load_capability_manifest(path, digest, bundle_dir=self.bundle)
            self.assertEqual(self.capability["nearRtRicId"], accepted["nearRtRicId"])
            path.write_bytes(encoded + b"\n")
            with self.assertRaises(CapabilityError):
                load_capability_manifest(path, digest, bundle_dir=self.bundle)

    def valid_inventory(self):
        sha = "a" * 64
        connections = []
        for index, static in enumerate(self.capability["e2Deployment"]["nodes"]):
            functions = []
            for required in static["requiredRanFunctions"]:
                is_kpm = required["ranFunctionId"] == 2
                capability = (
                    {
                        "reportStyles": [
                            {
                                "styleType": 1,
                                "actionDefinitionFormat": 1,
                                "indicationHeaderFormat": 1,
                                "indicationMessageFormat": 1,
                                "measurements": [{"name": "RRU.PrbDl", "measurementType": "NAME", "labels": []}],
                            },
                            {
                                "styleType": 4,
                                "actionDefinitionFormat": 4,
                                "indicationHeaderFormat": 1,
                                "indicationMessageFormat": 3,
                                "measurements": [{"name": "RRU.PrbDl", "measurementType": "NAME", "labels": []}],
                            },
                        ]
                    }
                    if is_kpm
                    else {
                        "styleType": 3,
                        "actionId": 1,
                        "headerFormat": 1,
                        "messageFormat": 1,
                        "outcomeFormat": 1,
                        "ranParameterTree": [
                            {
                                "id": 1,
                                "name": "targetPrimaryCellId",
                                "valueType": "STRUCTURE",
                                "mandatory": True,
                                "minOccurs": 1,
                                "maxOccurs": 1,
                                "children": [],
                            }
                        ],
                        "nrCgiEncodingProfileSha256": sha,
                    }
                )
                functions.append(
                    {
                        "ranFunctionId": required["ranFunctionId"],
                        "ranFunctionRevision": required["observedRanFunctionRevision"],
                        "ranFunctionOid": required["ranFunctionOid"],
                        "shortName": "ORAN-E2SM-KPM" if is_kpm else "ORAN-E2SM-RC",
                        "serviceModelVersion": "2.03" if is_kpm else "1.03",
                        "rawDefinition": {"path": "raw.bin", "sha256": required["rawDefinitionSha256"]},
                        "decodedDefinition": {"path": "decoded.json", "sha256": sha},
                        "canonicalDefinition": {
                            "algorithm": "RFC8785_JSON",
                            "toolVersion": "test",
                            "sha256": required["canonicalDecodedDefinitionSha256"],
                        },
                        "moduleSetSha256": sha,
                        "observedAt": "2026-08-04T00:00:00Z",
                        "active": True,
                        "capability": capability,
                    }
                )
            connections.append(
                {
                    "globalE2NodeId": copy.deepcopy(static["globalE2NodeId"]),
                    "connectionEpoch": 1,
                    "associationId": f"association-{index}",
                    "acceptedSetupAt": "2026-08-04T00:00:00Z",
                    "e2apVersion": "2.03",
                    "transferSyntax": "APER",
                    "rawE2SetupPdu": {"path": "setup.bin", "sha256": sha},
                    "decoderModuleSetSha256": sha,
                    "active": True,
                    "ranFunctions": functions,
                }
            )
        return {
            "schemaVersion": "oran-aic-e2-capability-inventory/1.0.0",
            "contractProfile": "oran-aic/1.0.0",
            "releaseManifestSha256": sha,
            "generatedAt": "2026-08-04T00:00:00Z",
            "status": "READY",
            "connections": connections,
        }

    @staticmethod
    def _write_artifact(directory: Path, name: str, document: dict) -> tuple[str, str]:
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        (directory / name).write_bytes(encoded)
        return name, hashlib.sha256(encoded).hexdigest()

    def valid_release_manifest(self):
        sha = "a" * 64
        artifact = {"path": "artifacts/item", "sha256": sha}
        build_artifact = {
            "artifactManifestPath": "artifacts/manifest.json",
            "artifactManifestSha256": sha,
            "binaries": [artifact],
        }
        provenance = self.capability["softwareProvenance"]
        module_names = [
            "_3gpp-common-subnetwork", "_3gpp-common-managed-element",
            "_3gpp-common-measurements", "_3gpp-common-yang-types", "_3gpp-common-top",
            "_3gpp-common-yang-extensions", "_3gpp-common-files", "_3gpp-common-subscription-control",
            "_3gpp-common-fm", "_3gpp-common-trace", "_3gpp-5gc-nrm-configurable5qiset",
            "_3gpp-5gc-nrm-ecmconnectioninfo", "ietf-inet-types", "ietf-yang-types",
            "ietf-yang-schema-mount", "ietf-yang-library",
        ]
        o1_component = {"implementationArtifacts": [artifact], "configurationArtifacts": [artifact]}
        modules = [
            {
                "name": name,
                "revision": "2026-01-01",
                "namespace": f"https://example.test/yang/{name}",
                "path": f"yang/{name}.yang",
                "sha256": sha,
            }
            for name in module_names
        ]
        profile_digests = {
            "E2SM-RC-STYLE3-ACTION1": provenance["rcProfileArtifact"]["byteSha256"],
            "E2SM-KPM-MEASUREMENT": sha,
            "E2-CONTROL-APER-VECTORS": provenance["aperVectorManifest"]["byteSha256"],
            "OTA-READBACK-EVIDENCE": provenance["otaEvidenceManifest"]["byteSha256"],
        }
        topology_nodes = []
        for node in self.capability["e2Deployment"]["nodes"]:
            identity = copy.deepcopy(node["globalE2NodeId"])
            topology_nodes.append(
                {
                    "role": node["role"],
                    **identity,
                    "nci": {"hex": "0x01", "bitLength": 8},
                    "managedObjectDn": "ManagedElement=example",
                    "oaiBuildArtifactManifestSha256": provenance["buildArtifactManifestSha256"],
                }
            )
        return {
            "schemaVersion": "oran-aic-backend-release-manifest/1.0.0",
            "contractProfile": "oran-aic/1.0.0",
            "releaseId": "a5c0564e-b7b7-4942-8a2d-f0c1d998bb74",
            "createdAt": "2026-08-04T00:00:00Z",
            "backendSource": {"repository": "https://example.test/backend", "commit": "b" * 40, "sourceTree": "c" * 40, "dirty": False},
            "oai": {
                "repository": "https://gitlab.eurecom.fr/oai/openairinterface5g.git",
                "release": provenance["oaiBaseTag"],
                "baseCommit": provenance["oaiBaseCommitSha1"],
                "patches": [{"path": item["path"], "sha256": item["byteSha256"]} for item in provenance["orderedPatchSet"]],
                "postPatchTree": provenance["postPatchSourceTreeSha1"],
                "buildArtifact": {**build_artifact, "artifactManifestSha256": provenance["buildArtifactManifestSha256"]},
            },
            "flexric": {
                "repository": "https://github.com/duranta-project/flexric.git",
                "commit": provenance["flexRicCommitSha1"],
                "submoduleGitlinkCommit": provenance["flexRicCommitSha1"],
                "patches": [], "postPatchTree": "d" * 40, "buildArtifact": build_artifact,
            },
            "build": {"e2apVersionEnum": "E2AP_V2", "kpmVersionEnum": "KPM_V2_03", "e2apEncoding": "ASN", "kpmEncoding": "ASN", "rcEncoding": "ASN", "compiler": "cc", "compilerVersion": "1", "cmakeVersion": "1", "platform": "test", "configureCommandSha256": sha},
            "asn1ModuleSets": [
                {"model": model, "specVersion": "1.03" if model == "E2SM-RC" else "2.03", "modules": [{"name": model, "path": f"asn1/{model}", "sha256": sha}], "aggregateSha256": sha, "compiler": "cc", "compilerVersion": "1", "codecArtifactSha256": sha}
                for model in ("E2AP", "E2SM-KPM", "E2SM-RC")
            ],
            "profiles": [{"name": name, "version": "1", "path": f"profiles/{name}", "sha256": digest} for name, digest in profile_digests.items()],
            "o1": {
                "yangModuleClosure": {"threeGppSnapshotCommitSha1": "dfada043e1a54453af779367615ad71e2a631268", "modules": modules, "aggregateSha256": sha, "enabledFeatures": [{"module": "_3gpp-common-managed-element", "feature": "MeasurementsUnderManagedElement"}], "schemaMountConfiguration": artifact, "mountedYangLibrarySnapshot": artifact, "closureValidationReport": artifact, "validationRule": "ALL_IMPORTS_RESOLVE_OFFLINE_AND_MOUNTED_YANG_LIBRARY_EXACTLY_MATCHES_RELEASE_CLOSURE"},
                "netconfYangProvider": {"buildArtifact": build_artifact, "configurationArtifacts": [artifact]},
                "netconfYangProfile": {"profileId": "oran-aic-o1-netconf-perfmetricjob/1.0.0", "path": "profiles/netconf", "sha256": sha},
                "performanceAssuranceProfile": {"profileId": "oran-aic-o1-pa-file/1.0.0", "path": "profiles/pa", "sha256": sha},
                "fileDataReportingProvider": o1_component, "pmFileGenerator": o1_component,
                "pmMeasurementMapping": artifact, "notificationSender": o1_component, "sftpProvider": o1_component,
            },
            "deployment": {"nearRtRicId": self.capability["nearRtRicId"], "e2BindAddress": "127.0.0.1", "e2SctpPort": 36421, "a1ListenEndpoint": "https://127.0.0.1:8443", "tlsTrustRef": "env://trust", "tlsIdentityRef": "env://identity", "serviceModelLibraryDir": "models"},
            "topology": {"nodes": topology_nodes},
        }

    def test_e2_inventory_ready_and_sc099_to_sc101_fail_closed(self):
        inventory = self.valid_inventory()
        self.assertTrue(validate_e2_inventory(inventory, self.capability, bundle_dir=self.bundle).ready)
        inactive_connection = copy.deepcopy(inventory)
        inactive_connection["connections"][0]["active"] = False
        self.assertFalse(validate_e2_inventory(inactive_connection, self.capability, bundle_dir=self.bundle).ready)
        inactive_function = copy.deepcopy(inventory)
        inactive_function["connections"][0]["ranFunctions"][0]["active"] = False
        self.assertFalse(validate_e2_inventory(inactive_function, self.capability, bundle_dir=self.bundle).ready)
        duplicate_node = copy.deepcopy(inventory)
        duplicate_node["connections"][1]["globalE2NodeId"] = copy.deepcopy(
            duplicate_node["connections"][0]["globalE2NodeId"]
        )
        result = validate_e2_inventory(duplicate_node, self.capability, bundle_dir=self.bundle)
        self.assertFalse(result.ready)
        self.assertEqual("AIC_E2_INVENTORY_NOT_READY", result.error_code)

    def test_capability_artifacts_loads_pinned_release_inventory_and_a1_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capability_name, capability_digest = self._write_artifact(root, "capability.json", self.capability)
            release_name, release_digest = self._write_artifact(root, "release.json", self.valid_release_manifest())
            inventory = self.valid_inventory()
            inventory["releaseManifestSha256"] = release_digest
            inventory_name, inventory_digest = self._write_artifact(root, "inventory.json", inventory)
            values = {
                "backend.capabilityManifestPath": capability_name,
                "backend.capabilityManifestSha256": capability_digest,
                "backend.releaseManifestPath": release_name,
                "backend.releaseManifestSha256": release_digest,
                "backend.e2CapabilityInventoryPath": inventory_name,
                "backend.e2CapabilityInventorySha256": inventory_digest,
            }
            # The shape a conformant A1-P v2 producer actually returns: the two
            # members the frozen contract defines, and nothing else.  The live
            # Lower A1-P Producer 1.0.0 was measured returning exactly this.
            discovery = dict(contract_policy_type_object(),
                             policyTypeId="AIC_UECellSteering_1.0.0")
            artifacts = CapabilityArtifacts.load(values, bundle_dir=self.bundle, base_dir=root, a1_discovery=discovery)
            self.assertTrue(artifacts.a1_ready)

            # Regression: the removed requirement.  A producer that carries no
            # RIC identity on this resource - which is every conformant one - is
            # admitted, and one that carries a foreign identity is *still*
            # admitted, because the contract does not define that member here
            # and the binding is made from the schemas instead.
            self.assertNotIn("nearRtRicId", discovery)
            foreign_identity = dict(discovery, nearRtRicId="other-ric")
            CapabilityArtifacts.load(values, bundle_dir=self.bundle, base_dir=root,
                                     a1_discovery=foreign_identity)

            # The re-anchor, in both directions: a producer serving another
            # release's schemas is refused, and so is one that omits a member
            # the contract requires.  A self-asserted digest is not a substitute
            # for the bytes, so a digest-only response is refused too.
            for member, pinned in (("policySchema", "policy"),
                                   ("statusSchema", "status")):
                other = copy.deepcopy(discovery)
                other[member] = dict(other[member], title="a different release")
                with self.assertRaisesRegex(
                        CapabilityError, f"A1 discovery {pinned} schema digest mismatch"):
                    CapabilityArtifacts.load(values, bundle_dir=self.bundle,
                                             base_dir=root, a1_discovery=other)
                absent = {key: value for key, value in discovery.items()
                          if key != member}
                with self.assertRaisesRegex(
                        CapabilityError, f"A1 discovery omitted the contract member {member}"):
                    CapabilityArtifacts.load(values, bundle_dir=self.bundle,
                                             base_dir=root, a1_discovery=absent)
            digests_only = {
                "policyTypeId": "AIC_UECellSteering_1.0.0",
                "policySchemaSha256": self.capability["schemaDigests"]["policy"],
                "statusSchemaSha256": self.capability["schemaDigests"]["status"],
            }
            with self.assertRaisesRegex(
                    CapabilityError, "A1 discovery omitted the contract member policySchema"):
                CapabilityArtifacts.load(values, bundle_dir=self.bundle,
                                         base_dir=root, a1_discovery=digests_only)


if __name__ == "__main__":
    unittest.main()
