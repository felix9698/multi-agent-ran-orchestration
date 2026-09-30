"""The R1 client's frozen profile, widened to the four Campaign 5 types.

Widening is deliberate and it does not weaken the steering path: each type is
still validated against its OWN policy/status schema, and any type outside the
union is still refused.  Tests use either a fake transport or an in-process
loopback server; neither reaches an external network or lab service.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from oran.rapp.r1_client import (
    FROZEN_POLICY_TYPES,
    HttpResponse,
    R1Client,
    R1Error,
    UrllibTransport,
)
from oran.campaign5.producer import Campaign5PolicyProducer, _Campaign5HttpServer

CAP = "AIC_UeDlPrbCap_1.0.0"
CAP_POLICY = {
    "config": {"cellId": "cell-1", "ueId": "ue-1", "maxDlPrbs": 12},
    "validity": {"notBefore": "2026-09-02T00:00:00Z", "notAfter": "2026-09-03T00:00:00Z"},
    "trace": {"traceId": "tx-1", "revision": 1, "fencingToken": 1},
}


def _valid_cap_status():
    producer = Campaign5PolicyProducer()
    producer.put_policy(CAP, "p1", CAP_POLICY)
    producer.record_applied(CAP, "p1", control_ack=True, observed_config=CAP_POLICY["config"])
    return producer.get_status(CAP, "p1")


class FakeTransport:
    """Routes by method and path suffix; every response echoes the Version."""

    def __init__(self, status_body):
        self.status_body = status_body
        self.requests = []

    def request(self, method, url, *, headers, body, timeout):
        del timeout
        self.requests.append((method, url, body))
        version = headers.get("Version", "")
        if method == "POST" and url.endswith("/policies"):
            return HttpResponse(201, {"Version": version,
                                      "Location": "/a1-policy-management/v1/policies/pol-1"},
                                {"ok": True})
        if method == "PUT" and url.endswith("/policies/pol-1"):
            return HttpResponse(200, {"Version": version}, {"ok": True})
        if method == "GET" and url.endswith("/policies/pol-1/status"):
            return HttpResponse(200, {"Version": version}, self.status_body)
        if method == "GET" and "/policy-types/" in url:
            return HttpResponse(200, {"Version": version}, {"policyTypeId": CAP})
        return HttpResponse(404, {"Version": version}, {"error": "unexpected"})


class WidenedProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.transport = FakeTransport(_valid_cap_status())
        self.client = R1Client(
            api_root="http://127.0.0.1/r1", r_app_id="rapp-test",
            policy_evidence_push_base_uri="http://127.0.0.1/push",
            state_path=Path(self.tmp.name) / "state.json",
            insecure_dev=True, transport=self.transport, retry_backoff_s=(0.0,),
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_frozen_profile_is_steering_plus_the_four_new_types(self):
        self.assertEqual(FROZEN_POLICY_TYPES[0], "AIC_UECellSteering_1.0.0")
        self.assertEqual(set(FROZEN_POLICY_TYPES[1:]), {
            "AIC_UeDlPrbCap_1.0.0", "AIC_SchedulerPriority_1.0.0",
            "AIC_DlMcsBounds_1.0.0", "AIC_CellDlTxPower_1.0.0",
        })

    def test_get_policy_type_accepts_a_new_type_and_refuses_an_unknown_one(self):
        self.client.get_policy_type(CAP)  # no raise
        with self.assertRaisesRegex(R1Error, "frozen profile"):
            self.client.get_policy_type("AIC_NotAThing_1.0.0")

    def test_create_policy_validates_a_new_type_against_its_own_schema(self):
        created = self.client.create_policy("nearRT-RIC", CAP, CAP_POLICY)
        self.assertEqual(created["policyId"], "pol-1")
        # An invalid body for the new type is rejected before any POST.
        bad = {**CAP_POLICY, "config": {"cellId": "c", "ueId": "u", "maxDlPrbs": 999}}
        with self.assertRaises(Exception):
            self.client.create_policy("nearRT-RIC", CAP, bad)

    def test_create_policy_refuses_a_type_outside_the_profile(self):
        with self.assertRaisesRegex(R1Error, "frozen profile"):
            self.client.create_policy("nearRT-RIC", "AIC_NotAThing_1.0.0", CAP_POLICY)

    def test_status_of_a_new_type_is_validated_against_its_own_status_schema(self):
        self.client.create_policy("nearRT-RIC", CAP, CAP_POLICY)
        status = self.client.get_policy_status("pol-1")
        self.assertEqual(status["aicStatus"]["readback"]["result"], "VERIFIED")

    def test_status_of_a_new_type_rejects_a_steering_shaped_body(self):
        # A body that is valid steering but not valid for the tracked new type
        # must be refused, proving the per-type dispatch is real.
        self.client.create_policy("nearRT-RIC", CAP, CAP_POLICY)
        self.transport.status_body = {"enforceStatus": "ENFORCED",
                                      "aicStatus": {"policyId": "pol-1"}}
        with self.assertRaises(Exception):
            self.client.get_policy_status("pol-1")


class LostDeleteReplyTransport:
    """The first DELETE removes the policy but its reply never arrives."""

    def __init__(self, first_delete_error):
        self.first_delete_error = first_delete_error
        self.deletes = 0

    def request(self, method, url, *, headers, body, timeout):
        del url, body, timeout
        assert method == "DELETE"
        self.deletes += 1
        if self.deletes == 1 and self.first_delete_error is not None:
            raise self.first_delete_error
        return HttpResponse(404, {"Version": headers.get("Version", "")},
                            {"error": "unknown policy"})


class ADeleteWhoseReplyWasLost(unittest.TestCase):
    def _client(self, transport, tmp):
        return R1Client(
            api_root="http://127.0.0.1/r1", r_app_id="rapp-test",
            policy_evidence_push_base_uri="http://127.0.0.1/push",
            state_path=Path(tmp) / "state.json", insecure_dev=True,
            transport=transport, retry_backoff_s=(0.0,),
        )

    def test_a_retry_that_finds_the_policy_gone_is_a_delete(self):
        transport = LostDeleteReplyTransport(TimeoutError("reply lost"))
        with tempfile.TemporaryDirectory() as tmp:
            self._client(transport, tmp).delete_policy("pol-1")
        self.assertEqual(transport.deletes, 2)

    def test_a_first_attempt_404_is_still_an_error(self):
        transport = LostDeleteReplyTransport(None)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(R1Error, "404"):
                self._client(transport, tmp).delete_policy("pol-1")


class RecordingUrllibTransport:
    """Use the real HTTP codec while retaining the R1 wire sequence."""

    def __init__(self):
        self.delegate = UrllibTransport()
        self.requests = []

    def request(self, method, url, *, headers, body, timeout):
        self.requests.append((method, urlsplit(url).path, headers.get("Version")))
        return self.delegate.request(
            method, url, headers=headers, body=body, timeout=timeout
        )


class MockVerifiedWorker:
    """Synchronous live-worker seam with an observable restored baseline."""

    def __init__(self, producer):
        self.producer = producer
        self.capability = None
        self.live = {}
        self.apply_calls = []

    def bind(self):
        self.capability = self.producer.bind_live_worker(
            self.apply, self.delete
        )

    def apply(self, policy_type_id, policy_id, policy):
        self.apply_calls.append(policy_id)
        self.live[policy_id] = dict(policy["config"])
        self.producer.record_applied(
            policy_type_id, policy_id, control_ack=True,
            observed_config=policy["config"],
        )

    def delete(self, policy_id):
        record = self.producer.policy_record(policy_id)
        self.live[policy_id] = {
            "cellId": record["policy"]["config"]["cellId"],
            "ueId": record["policy"]["config"]["ueId"],
            "maxDlPrbs": 0,
        }
        self.producer.delete_after_rollback(
            record["policyTypeId"], policy_id, self.capability
        )


class RealR1ClientToProducerFacade(unittest.TestCase):
    """The Cockpit's actual R1 client crosses the in-process HTTP facade."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.producer = Campaign5PolicyProducer()
        self.worker = MockVerifiedWorker(self.producer)
        self.worker.bind()
        self.server = _Campaign5HttpServer(
            ("127.0.0.1", 0), self.producer, "test-token"
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        self.transport = RecordingUrllibTransport()
        self.root = f"http://127.0.0.1:{self.server.server_port}"
        self.client = R1Client(
            api_root=self.root + "/r1",
            r_app_id="rapp-campaign5-test",
            policy_evidence_push_base_uri=self.root + "/push",
            state_path=Path(self.tmp.name) / "r1-state.json",
            insecure_dev=True,
            oauth_header_provider=lambda _method, _url, _version: "Bearer test-token",
            transport=self.transport,
            timeout_s=1.0,
            retry_backoff_s=(0.0,),
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)
        self.tmp.cleanup()

    def test_full_policy_lifecycle_uses_one_store_and_restores_on_delete(self):
        self.assertEqual(self.client.bootstrap_info()["bootstrap"], "ready")
        services = self.client.discover_services(api_name="a1-policy-management")
        self.assertEqual(services[0]["apiName"], "a1-policy-management")
        self.assertEqual(self.client.get_policy_type(CAP)["policyTypeId"], CAP)
        self.assertIn(CAP, self.client.discover_policy_types())
        self.client.get_policy_type(CAP)

        created = self.client.create_policy("near-rt-ric-test", CAP, CAP_POLICY)
        policy_id = created["policyId"]
        status = self.client.get_policy_status(policy_id)
        self.assertEqual(status["aicStatus"]["episodeState"], "APPLIED_VERIFIED")
        self.assertEqual(status["aicStatus"]["readback"]["result"], "VERIFIED")
        self.assertEqual(
            self.client.get_policy(policy_id)["policyObject"], CAP_POLICY
        )
        self.assertEqual(
            self.client.list_policies(
                near_rt_ric_id="near-rt-ric-test", policy_type_id=CAP
            )[0]["policyObject"],
            self.producer.get_policy(CAP, policy_id),
        )

        self.client.delete_policy(policy_id)
        self.assertEqual(self.worker.live[policy_id]["maxDlPrbs"], 0)
        self.assertEqual(
            self.producer.handle(
                "GET", f"/A1-P/v2/policytypes/{CAP}/policies/{policy_id}"
            ).status,
            404,
        )

        recorded = json.loads(
            Path(
                "docs/integration/evidence/"
                "LIVECONSOLE-UeCellSteeringPinToCell-20260904T102054Z-run.json"
            ).read_text(encoding="utf-8")
        )
        expected_live_prefix = [
            call["method"] for call in recorded["transportCalls"][:6]
        ]
        wire_names = {
            ("GET", "/r1/bootstrap/v1/bootstrap-info"): "bootstrap_info",
            ("GET", "/r1/service-apis/v1/allServiceAPIs"): "discover_services",
            ("GET", f"/r1/a1-policy-management/v1/policy-types/{CAP}"): "get_policy_type",
            ("GET", "/r1/a1-policy-management/v1/policy-types"): "discover_policy_types",
            ("POST", "/r1/a1-policy-management/v1/policies"): "create_policy",
        }
        actual_live_prefix = [
            wire_names[(method, path)]
            for method, path, _version in self.transport.requests[:6]
        ]
        self.assertEqual(actual_live_prefix, expected_live_prefix)
        self.assertEqual(
            [(method, path) for method, path, _ in self.transport.requests[-3:]],
            [
                ("GET", f"/r1/a1-policy-management/v1/policies/{policy_id}"),
                ("GET", "/r1/a1-policy-management/v1/policies"),
                ("DELETE", f"/r1/a1-policy-management/v1/policies/{policy_id}"),
            ],
        )

    def test_second_policy_for_the_same_semantic_scope_is_refused(self):
        self.client.create_policy("near-rt-ric-test", CAP, CAP_POLICY)
        second = {
            **CAP_POLICY,
            "trace": {"traceId": "tx-2", "revision": 1, "fencingToken": 1},
        }
        with self.assertRaisesRegex(R1Error, "409"):
            self.client.create_policy("near-rt-ric-test", CAP, second)
        self.assertEqual(len(self.producer.list_policies(CAP)), 1)

    def test_retryable_create_converges_on_one_a1_resource(self):
        first = self.client.create_policy("near-rt-ric-test", CAP, CAP_POLICY)
        second = self.client.create_policy("near-rt-ric-test", CAP, CAP_POLICY)
        self.assertEqual(second["policyId"], first["policyId"])
        self.assertEqual(
            self.producer.list_policies(CAP), [first["policyId"]]
        )
        self.assertEqual(self.worker.apply_calls, [first["policyId"]])

    def test_post_is_create_only_and_both_views_share_explicit_put_updates(self):
        created = self.client.create_policy("near-rt-ric-test", CAP, CAP_POLICY)
        policy_id = created["policyId"]
        changed = {
            **CAP_POLICY,
            "config": {**CAP_POLICY["config"], "maxDlPrbs": 8},
            "trace": {"traceId": "tx-1", "revision": 2, "fencingToken": 2},
        }

        with self.assertRaisesRegex(R1Error, "409"):
            self.client.create_policy("near-rt-ric-test", CAP, changed)
        self.assertEqual(self.producer.get_policy(CAP, policy_id), CAP_POLICY)

        updated = self.client.update_policy(policy_id, changed)
        self.assertEqual(updated["policyObject"], changed)
        a1_view = self.producer.handle(
            "GET", f"/A1-P/v2/policytypes/{CAP}/policies/{policy_id}"
        )
        self.assertEqual(a1_view.status, 200)
        self.assertEqual(a1_view.body, changed)

    def test_a1_created_policy_remains_a_member_of_the_r1_ric_filter(self):
        response = self.producer.handle(
            "PUT", f"/A1-P/v2/policytypes/{CAP}/policies/a1-created", CAP_POLICY
        )
        self.assertEqual(response.status, 201)

        listed = self.client.list_policies(
            near_rt_ric_id="near-rt-ric-test", policy_type_id=CAP
        )
        self.assertEqual([item["policyObject"] for item in listed], [CAP_POLICY])
        self.assertIsNone(listed[0]["nearRtRicId"])

    def test_unknown_type_and_missing_bearer_are_refused_before_any_effect(self):
        before = len(self.transport.requests)
        with self.assertRaisesRegex(R1Error, "frozen profile"):
            self.client.get_policy_type("AIC_NotAThing_1.0.0")
        self.assertEqual(len(self.transport.requests), before)

        producer_refusal = self.transport.request(
            "GET",
            self.root + "/r1/a1-policy-management/v1/policy-types/AIC_NotAThing_1.0.0",
            headers={"Authorization": "Bearer test-token", "Version": "1.0.0"},
            body=None,
            timeout=1.0,
        )
        self.assertEqual(producer_refusal.status, 404)

        unauthenticated = R1Client(
            api_root=self.root + "/r1",
            r_app_id="rapp-campaign5-test",
            policy_evidence_push_base_uri=self.root + "/push",
            state_path=Path(self.tmp.name) / "unauthenticated-state.json",
            insecure_dev=True,
            timeout_s=1.0,
            retry_backoff_s=(0.0,),
        )
        with self.assertRaisesRegex(R1Error, "401"):
            unauthenticated.create_policy("near-rt-ric-test", CAP, CAP_POLICY)
        self.assertEqual(self.producer.list_policies(CAP), [])


if __name__ == "__main__":
    unittest.main()


class ProducerHonoursRotatedSecretFile(unittest.TestCase):
    """The R1 access token is rotated on disk by the token refresher; the producer must
    follow the file per request and keep the previous token only for a short grace window."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.secret = Path(self.tmp.name) / "r1-access-token"
        self.secret.write_text("token-one\n", encoding="utf-8")
        self.producer = Campaign5PolicyProducer()

    def tearDown(self):
        self.tmp.cleanup()

    def _server(self, grace):
        server = _Campaign5HttpServer(
            ("127.0.0.1", 0), self.producer, "token-one",
            secret_path=self.secret, rotation_grace_seconds=grace,
        )
        self.addCleanup(server.server_close)
        return server

    def _rotate(self, token):
        import os
        self.secret.write_text(token + "\n", encoding="utf-8")
        stat = self.secret.stat()
        os.utime(self.secret, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    def test_rotated_token_is_accepted_and_previous_token_only_within_grace(self):
        server = self._server(grace=60.0)
        self.assertTrue(server.accepts_authorization("Bearer token-one"))
        self.assertFalse(server.accepts_authorization("Bearer token-two"))
        self._rotate("token-two")
        self.assertTrue(server.accepts_authorization("Bearer token-two"))
        self.assertTrue(server.accepts_authorization("Bearer token-one"))
        self.assertFalse(server.accepts_authorization("Bearer token-three"))

    def test_previous_token_is_refused_once_the_grace_window_has_passed(self):
        server = self._server(grace=0.0)
        self._rotate("token-two")
        self.assertTrue(server.accepts_authorization("Bearer token-two"))
        self.assertFalse(server.accepts_authorization("Bearer token-one"))

    def test_unreadable_or_malformed_rotation_keeps_the_last_good_token(self):
        server = self._server(grace=60.0)
        self._rotate("two\nlines")
        self.assertTrue(server.accepts_authorization("Bearer token-one"))
        self.secret.unlink()
        self.assertTrue(server.accepts_authorization("Bearer token-one"))

    def test_server_without_secret_path_keeps_the_static_token(self):
        server = _Campaign5HttpServer(("127.0.0.1", 0), self.producer, "static")
        self.addCleanup(server.server_close)
        self.assertTrue(server.accepts_authorization("Bearer static"))
        self.assertFalse(server.accepts_authorization("Bearer other"))


class DefinitiveStatusesAreRefusalsNotLostMessages(unittest.TestCase):
    """400/404/409 mean the producer answered; 5xx and 408/429 do not.

    The distinction is not cosmetic.  A write classified UNKNOWN keeps its
    scope reserved so a recovery can run, which is right for a lost message and
    wrong for a decision: live on 2026-09-17 a 409 arrived as a plain transport
    error, the failed trial held the scope RESERVED, and the NEXT trial on the
    same axis was rejected with "scope is still owned by transaction ...".
    """

    def test_a_conflict_carries_its_status_and_is_an_R1Error(self):
        from oran.rapp.r1_client import R1Error, R1Refusal, R1_DEFINITIVE_STATUSES
        for status in sorted(R1_DEFINITIVE_STATUSES):
            refusal = R1Refusal(f"PUT /policies returned {status}", status=status)
            self.assertIsInstance(refusal, R1Error)
            self.assertEqual(status, refusal.status)

    def test_the_retryable_statuses_are_not_definitive(self):
        from oran.rapp.r1_client import R1_DEFINITIVE_STATUSES
        for status in (408, 425, 429, 500, 502, 503, 504):
            self.assertNotIn(status, R1_DEFINITIVE_STATUSES)

    def test_the_live_adapter_counts_a_refusal_among_its_refusal_errors(self):
        from oran.rapp.r1_client import R1Refusal
        import tools.liveconsole.build as build
        source = build.__file__
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("R1Refusal", text,
                      "the supplementary adapter must classify a producer "
                      "decision as a refusal, not as a lost message")
        self.assertTrue(issubclass(R1Refusal, Exception))


class TheLiveBuilderWrapperMustNotSwallowTheRevisionSeed(unittest.TestCase):
    """``controlled_scope_builder`` has to forward ``last_revision``.

    The adapter inspects the wrapper's signature **once**, at construction
    (``_accepts_last_revision``), and a wrapper without the parameter makes it
    skip the durable seed for the whole run -- silently, because nothing fails:
    bodies just come from the builder's own numbering.  Live on 2026-09-17 that
    made every takeover PUT carry the revision the adopted policy already held
    and the producer refused each one ("policy update requires a newer revision
    and fencingToken"); the journal said ``seed=None``, i.e. the seed was never
    asked for.  The hermetic fixture's wrapper had always forwarded it, so the
    seed logic passed its own tests while doing nothing on the bed.
    """

    def _builder(self):
        from tools.liveconsole.build import controlled_scope_builder
        from oran.campaign5 import CAMPAIGN5_FAMILIES

        class _Declared:
            axis = "pfWeight"

            @staticmethod
            def wire_value(value):
                return value

        return controlled_scope_builder(
            CAMPAIGN5_FAMILIES["priority"], _Declared,
            validity_provider=lambda command: {"notBefore": "x", "notAfter": "y"})

    def test_the_adapter_recognises_the_seed_parameter(self):
        from assurance.gateway.r1_adapter import _accepts_last_revision
        self.assertTrue(
            _accepts_last_revision(self._builder()),
            "the adapter would skip the durable revision seed for the whole run")

    def test_the_parameter_is_keyword_only_and_optional(self):
        import inspect
        spec = inspect.getfullargspec(self._builder())
        self.assertIn("last_revision", spec.kwonlyargs)
        self.assertEqual(1, len(spec.args), "the command stays the only positional")
