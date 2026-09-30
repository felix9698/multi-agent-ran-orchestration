import copy
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from oran.rapp.contract_support import _bundle_dir
from oran.rapp.http_servers import RAppHttpServer
from oran.rapp.r1_client import R1Client


class _StubState:
    def __init__(self, golden):
        self.golden = golden
        self.requests = []
        self.policy = golden["policy"]
        self.status_queries = 0
        self.pull_polls = 0
        self.pull_uri = None
        bundle = _bundle_dir()
        self.policy_type = {"policyTypeId": "AIC_UECellSteering_1.0.0",
            "policySchema": json.loads((bundle /
                "AIC_UECellSteering_1.0.0.policy.schema.json").read_text()),
            "statusSchema": json.loads((bundle /
                "AIC_UECellSteering_1.0.0.status.schema.json").read_text())}


def _handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *args):
            return

        def _body(self):
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length)) if length else None

        def _reply(self, status, body=None, *, location=None, extra_headers=None):
            raw = b"" if body is None else json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Version", self.headers.get("Version", ""))
            if location:
                self.send_header("Location", location)
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            if raw:
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            if raw:
                self.wfile.write(raw)

        def _record(self, body=None):
            state.requests.append((self.command, self.path,
                                   self.headers.get("Version"), body))

        def do_GET(self):
            self._record()
            path = self.path.split("?", 1)[0]
            if path == "/bootstrap/v1/bootstrap-info":
                self._reply(200, {"bootstrap": "ready"})
            elif path.startswith("/service-apis/v1/allServiceAPIs"):
                self._reply(200, [{"vendorSpecific-o-ran.org": {
                    "fullApiVersions": ["1.0.0"]}}])
            elif path == "/a1-policy-management/v1/policy-types":
                self._reply(200, ["AIC_UECellSteering_1.0.0"])
            elif path == ("/a1-policy-management/v1/policy-types/"
                          "AIC_UECellSteering_1.0.0"):
                self._reply(200, state.policy_type)
            elif path.endswith("/status") and "/policies/" in path:
                state.status_queries += 1
                self._reply(200, state.golden["appliedVerifiedStatus"])
            elif path.startswith("/data-discovery/v2/dme-types/"):
                self._reply(200, {"dmeTypeId": "aic:policy-evidence:1.0.0"})
            elif path == "/data-access/v2/data-jobs/job-1/status":
                self._reply(200, {"dataJobInfoStatus": "ACTIVE"})
            elif path == "/data-access/v2/data-jobs/job-1":
                self._reply(200, {"dataJobInfoStatus": "ACTIVE"})
            elif path.endswith("/subscriptions/sub-1"):
                self._reply(200, {"subscriptionId": "sub-1"})
            elif path == "/pull/1":
                state.pull_polls += 1
                if state.pull_polls == 1:
                    self._reply(202, extra_headers={"Retry-After": "0"})
                else:
                    self._reply(200, state.golden["afterEvidence"])
            else:
                self._reply(404, {"error": path})

        def do_POST(self):
            body = self._body()
            self._record(body)
            path = self.path
            if path == "/a1-policy-management/v1/policies":
                state.policy = body["policyObject"]
                self._reply(201, body,
                            location="/a1-policy-management/v1/policies/" +
                            state.golden["appliedVerifiedStatus"]["aicStatus"]
                            ["policyId"])
            elif path == "/a1-policy-management/v1/policies/subscriptions":
                self._reply(201, body, location=(
                    "/a1-policy-management/v1/policies/subscriptions/sub-1"))
            elif path == "/data-access/v2/data-jobs":
                response = dict(body, dataJobInfoStatus="ACTIVE")
                if body["dataDeliveryMode"] == "ONE_TIME":
                    response["pullDeliveryDetailsHttp"] = {
                        "dataPullUri": state.pull_uri}
                self._reply(201, response,
                            location="/data-access/v2/data-jobs/job-1")
            else:
                self._reply(404, {"error": path})

        def do_PUT(self):
            body = self._body()
            self._record(body)
            self._reply(200, body)

        def do_DELETE(self):
            self._record()
            self._reply(204)

    return Handler


class _CoordinatorView:
    def transition_snapshot(self):
        return [{"from": "S3", "to": "S4", "outcome": "SATISFIED",
                 "evidenceRef": "dme-observation:fixture"}]


class R1ClientStubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.golden = json.loads(
            (_bundle_dir() / "golden/golden-vectors.1.0.0.json")
            .read_text(encoding="utf-8"))["canonicalObjects"]

    def setUp(self):
        self.stub_state = _StubState(self.golden)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), _handler(self.stub_state))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = f"http://127.0.0.1:{self.server.server_port}"
        self.stub_state.pull_uri = self.root + "/pull/1"
        self.client = R1Client(
            api_root=self.root, r_app_id="rapp-fixture",
            policy_evidence_push_base_uri=self.root + "/dme-push",
            state_path=Path(self.tmp.name) / "r1-state.json",
            insecure_dev=True, timeout_s=1.0,
            retry_backoff_s=(0.0, 0.0, 0.0))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def test_pinned_policy_and_service_uris_and_versions(self):
        self.client.bootstrap_info()
        self.client.discover_services(api_name="a1-policy-management")
        self.assertEqual(self.client.discover_policy_types(),
                         ["AIC_UECellSteering_1.0.0"])
        created = self.client.create_policy(
            self.golden["capabilityManifest"]["nearRtRicId"],
            "AIC_UECellSteering_1.0.0", self.golden["policy"])
        self.assertEqual(created["policyId"],
                         self.golden["appliedVerifiedStatus"]["aicStatus"]
                         ["policyId"])
        self.client.update_policy(created["policyId"], self.golden["policy"])
        sub = self.client.create_status_subscription(
            self.root + "/r1/a1-policy-status", [created["policyId"]])
        self.client.get_status_subscription(sub["subscriptionId"])
        self.client.delete_status_subscription(sub["subscriptionId"])
        paths = [request[1] for request in self.stub_state.requests]
        self.assertIn("/bootstrap/v1/bootstrap-info", paths)
        self.assertTrue(any(path.startswith(
            "/service-apis/v1/allServiceAPIs?api-invoker-id=rapp-fixture")
                            for path in paths))
        self.assertNotIn("/policies", paths)

    def test_status_wrapper_epoch_query_and_dedupe(self):
        status = self.golden["appliedVerifiedStatus"]
        notification = {"subscriptionId": "sub-1", "policyStates": [{
            "policyId": status["aicStatus"]["policyId"],
            "policyStatusObject": status}]}
        self.assertEqual(self.client.handle_status_notification(notification), 1)
        self.assertEqual(self.stub_state.status_queries, 1)
        self.assertEqual(self.client.handle_status_notification(notification), 0)
        self.assertEqual(self.stub_state.status_queries, 1)

    def test_first_epoch_can_use_authoritative_provider_without_r1_query(self):
        status = self.golden["appliedVerifiedStatus"]
        policy_id = status["aicStatus"]["policyId"]
        provider_calls = []

        def authoritative_status_provider(candidate_policy_id):
            provider_calls.append(candidate_policy_id)
            return copy.deepcopy(status)

        try:
            client = R1Client(
                api_root=self.root, r_app_id="rapp-fixture",
                policy_evidence_push_base_uri=self.root + "/dme-push",
                state_path=Path(self.tmp.name) / "provider-r1-state.json",
                authoritative_status_provider=authoritative_status_provider,
                insecure_dev=True, timeout_s=1.0,
                retry_backoff_s=(0.0, 0.0, 0.0))
        except TypeError as exc:
            self.fail(f"R1Client rejected authoritative status provider: {exc}")

        notification = {"subscriptionId": "sub-1", "policyStates": [{
            "policyId": policy_id, "policyStatusObject": status}]}
        self.assertEqual(client.handle_status_notification(notification), 1)
        self.assertEqual(provider_calls, [policy_id])
        self.assertEqual(self.stub_state.status_queries, 0)
        self.assertEqual(
            client.state.snapshot()["status"][policy_id]["snapshot"], status)

        self.assertEqual(client.handle_status_notification(notification), 0)
        self.assertEqual(provider_calls, [policy_id])
        self.assertEqual(self.stub_state.status_queries, 0)

    def test_dme_item_discovery_durable_binding_and_callback_dedupe(self):
        self.client.discover_dme_type()
        job = self.client.create_continuous_job(
            policy_id=self.golden["afterEvidence"]["correlation"]["policyId"],
            policy_revision=1,
            near_rt_ric_id=self.golden["capabilityManifest"]["nearRtRicId"])
        state = self.client.state.snapshot()
        binding = state["bindings"][job["deliveryBindingId"]]
        self.assertEqual(binding["dataJobId"], "job-1")
        self.assertTrue(binding["active"])
        discover_path = next(path for method, path, _, _ in self.stub_state.requests
                             if "/data-discovery/" in path)
        self.assertEqual(discover_path,
                         "/data-discovery/v2/dme-types/"
                         "aic%3Apolicy-evidence%3A1.0.0")

        callbacks = RAppHttpServer(
            host="127.0.0.1", port=0, client=self.client,
            coordinator=_CoordinatorView(),
            status_callback_path="/r1/a1-policy-status",
            dme_push_base_path="/dme-push", insecure_dev=True,
            enable_harness=True)
        callbacks.start()
        try:
            port = callbacks.address[1]
            uri = f"http://127.0.0.1:{port}/dme-push/{job['deliveryBindingId']}"
            raw = json.dumps(self.golden["afterEvidence"]).encode("utf-8")
            for _ in range(2):
                request = Request(uri, data=raw, method="POST", headers={
                    "Content-Type": "application/json", "Version": "1.0.0"})
                with urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 204)
            evidence = self.client.state.snapshot()["evidence"]
            self.assertEqual(len(evidence), 1)

            harness_request = Request(
                f"http://127.0.0.1:{port}/harness/op",
                data=json.dumps({"op": "COORDINATOR_TRANSITION",
                                 "from": "S3", "to": "S4"}).encode(),
                method="POST", headers={"Content-Type": "application/json"})
            with urlopen(harness_request, timeout=2) as response:
                output = json.load(response)
            self.assertEqual(output, {"outputs": {
                "state": "S4", "outcome": "SATISFIED"}})
        finally:
            callbacks.close()

    def test_one_time_pull_honors_202_retry_after(self):
        payload = self.client.recover_one_time_pull(
            policy_id=self.golden["afterEvidence"]["correlation"]["policyId"],
            policy_revision=1,
            near_rt_ric_id=self.golden["capabilityManifest"]["nearRtRicId"],
            max_polls=2)
        self.assertEqual(payload, self.golden["afterEvidence"])
        self.assertEqual(self.stub_state.pull_polls, 2)


if __name__ == "__main__":
    unittest.main()


class ProblemDetailSurvivesTruncationTests(unittest.TestCase):
    """RFC7807 의 ``detail`` 이 절단선 안쪽에 남아야 한다.

    2026-09-17 의 조종 실패는 전부 ``'status': 503, 'deta`` 에서 끊겨 있었다.
    ``type`` 과 ``title`` 이 같은 코드를 두 번 말하며 앞을 채웠고, 원인을 담은
    유일한 칸이 잘려 나갔다.  게이트웨이는 400자에서 자르므로 그 안에 원인이
    들어오는지를 직접 잰다 — 절단폭을 늘리는 쪽은 끝이 없다.
    """

    GATEWAY_TRUNCATION = 400
    # 게이트웨이가 R1 문자열 앞에 붙이는 실제 접두사 (gateway.py / r1_adapter.py)
    PREFIX = ("commit: UNKNOWN an acknowledgement was lost and the baseline is "
              "still live; the write may yet land; servingCell@ue1: UNKNOWN "
              "APPLY failed: R1Error: ")

    BODY = {"type": "urn:oran-aic:problem:AIC_INTERNAL_ERROR",
            "title": "AIC_INTERNAL_ERROR", "status": 503,
            "detail": "pin-to-cell handoff refused: no live E2 node for gnb2",
            "instance": "urn:uuid:9f1d8c22-0b47-4a1e-9d2e-6c8f0a51b3ee"}

    def _client_error(self, status, retryable):
        from unittest import mock
        from oran.rapp.r1_client import HttpResponse, R1Client, R1Error, R1Refusal
        client = R1Client.__new__(R1Client)
        client.transport = mock.Mock()
        client.transport.request.return_value = HttpResponse(
            status=status, headers={"Version": "1.0.0"}, body=self.BODY)
        client.insecure_dev = False
        client.oauth_header_provider = None
        client.max_attempts = 2
        client.backoff = ()
        client.timeout_s = 1.0
        client.r_app_id = "test"
        with self.assertRaises((R1Error, R1Refusal)) as caught:
            client._request("POST",
                            "https://192.168.50.1:18443/r1/a1-policy-management"
                            "/v1/policies",
                            version="1.0.0", body={}, expected=(201,),
                            retryable=retryable, absolute=True)
        return str(caught.exception)

    def test_the_cause_survives_the_gateway_truncation(self):
        message = self.PREFIX + self._client_error(503, retryable=True)
        self.assertIn(self.BODY["detail"],
                      message[:self.GATEWAY_TRUNCATION],
                      "원인이 절단선 밖으로 밀려났다:\n" + message)

    def test_a_definitive_refusal_also_names_its_cause(self):
        message = self._client_error(409, retryable=False)
        self.assertIn(self.BODY["detail"], message)
        # type/instance 는 원인이 아니므로 자리를 차지하지 않는다
        self.assertNotIn("urn:uuid:", message)
        self.assertNotIn("urn:oran-aic:problem:", message)

    def test_a_body_that_is_not_a_problem_object_is_unchanged(self):
        from oran.rapp.r1_client import _problem_text
        self.assertEqual(_problem_text(b"raw bytes"), repr(b"raw bytes"))
        self.assertEqual(_problem_text({"a": 1}), repr({"a": 1}))

    def test_the_other_producer_speaks_plain_error(self):
        """이 배포에는 모양이 다른 프로듀서가 둘 있다.

        pin-to-cell(18443)은 RFC7807 이고, 캠페인5 액션 프로듀서(9445)는
        ``{"error": "..."}`` 하나만 보낸다.  2026-09-17 의 409/404 가 그 모양인데
        한쪽만 풀면 다른 쪽은 ``repr`` 로 떨어져 중괄호·따옴표가 절단선을 갉는다.
        """
        from oran.rapp.r1_client import _problem_text
        self.assertEqual(
            _problem_text({"error": "policy update requires a newer revision "
                                    "and fencingToken"}),
            "policy update requires a newer revision and fencingToken")
        self.assertEqual(_problem_text({"error": "unknown policy abc-123"}),
                         "unknown policy abc-123")
        # 빈 문자열은 사유가 아니므로 본문을 그대로 보여 준다
        self.assertEqual(_problem_text({"error": ""}), repr({"error": ""}))
