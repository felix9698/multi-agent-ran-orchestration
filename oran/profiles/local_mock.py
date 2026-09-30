"""Loopback-only composition root for the complete local O-RAN mock stack.

Every listen address and peer URI comes from the validated integration-values
document or its digest-pinned deployment vector.  This module intentionally has
no fallback ports, environment endpoint overrides, or legacy radio executors.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import signal
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlsplit
import base64

from decision.llm_backend import DeterministicMockBackend, LLMBackendManager
from oran.contract.integration_values import load_integration_values
from oran.conformance.contracts import ContractBundle, validate_json_schema
from oran.conformance.harness_api import HttpHarnessAdapter
from oran.conformance.vectors import local_development_vector
from oran.mocks.nearrt.producer import NearRtMock, validate_status
from oran.mocks.nearrt.server import create_server as create_nearrt_server
from oran.mocks.o1provider.provider import O1ProviderMock
from oran.o1.core import (
    DurableJson, HttpResult, NetconfPerfMetricJobManager, NotificationReceiver,
    O1Consumer, SubscriptionManager,
)
from oran.nonrt.__main__ import create_service_from_values
from oran.nonrt.http_server import NonRtHttpServer
from oran.rapp.assurance import CombinedAssurance
from oran.rapp.coordinator_adapter import RAppCoordinatorAdapter
from oran.rapp.evidence import EvidenceLedger
from oran.rapp.headless import _load_pinned_json
from oran.rapp.http_servers import RAppHttpServer
from oran.rapp.r1_client import R1Client


class LocalProfileError(ValueError):
    pass


class LocalMockNetconfBoundary:
    """Explicit runner boundary backed by the local O1 consumer's manager."""

    def __init__(self, vector: Mapping[str, Any]):
        endpoint = _endpoint(
            vector["o1"]["fileDataReporting"]["consumerReference"],
            "o1.fileDataReporting.consumerReference")
        self.client = HttpHarnessAdapter(endpoint.origin)

    def execute(self, step: Mapping[str, Any], *, vector: Mapping[str, Any],
                profile: Mapping[str, Any], timeout_ms: int | None) -> dict[str, Any]:
        if profile.get("profileId") != "oran-aic-o1-netconf-perfmetricjob/1.0.0":
            raise LocalProfileError("unexpected NETCONF profile")
        source = step.get("dependencySource")
        if step.get("action") == "UNLOCK" and isinstance(source, Mapping):
            self.client.operation("SET_DEPENDENCY", {
                "dependency": "DURABLE_SUBSCRIPTION_STATE",
                "ready": True,
                "source": source,
            })
        return self.client.operation("PERF_METRIC_JOB_BOUNDARY", {
            "action": step.get("action"), "timeoutMs": timeout_ms,
            "managedObjectUri": vector["o1"]["perfMetricJob"]["managedObjectUri"],
        })


class LocalMockSftpBoundary:
    """Explicit trust-checking SFTP boundary backed by the O1 Provider mock."""

    def __init__(self, vector: Mapping[str, Any]):
        endpoint = _endpoint(
            vector["o1"]["fileDataReporting"]["mnsRoot"],
            "o1.fileDataReporting.mnsRoot")
        self.client = HttpHarnessAdapter(endpoint.origin)
        self.last_retrieved_at: str | None = None

    def retrieve(self, source: str, *, vector: Mapping[str, Any],
                 timeout_ms: int | None) -> bytes:
        self.last_retrieved_at = None
        parsed = urlsplit(source)
        authority = (parsed.hostname if parsed.port is None
                     else "%s:%s" % (parsed.hostname, parsed.port))
        allowed = set(vector["o1"]["sftp"]["allowedAuthorities"])
        if (parsed.scheme != "sftp" or parsed.username or parsed.password
                or not authority or not parsed.path or ".." in Path(parsed.path).parts
                or (authority not in allowed and parsed.hostname not in allowed)):
            raise LocalProfileError("SFTP source violates the deployment trust boundary")
        outputs = self.client.operation("O1_SFTP_RETRIEVE", {
            "source": source,
            "connectTimeoutMs": min(timeout_ms or 5000, 5000),
            "readTimeoutMs": timeout_ms or 30000,
        })
        encoded = outputs.get("artifactBase64")
        if not isinstance(encoded, str):
            raise LocalProfileError("O1 Provider did not return SFTP bytes")
        retrieved_at = outputs.get("retrievedAt")
        if not isinstance(retrieved_at, str):
            raise LocalProfileError("O1 Provider did not return retrieval completion time")
        self.last_retrieved_at = retrieved_at
        try:
            return base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise LocalProfileError("O1 Provider returned invalid SFTP bytes") from exc


def local_mock_runner_boundaries(vector: Mapping[str, Any]) -> tuple[LocalMockSftpBoundary, LocalMockNetconfBoundary]:
    return LocalMockSftpBoundary(vector), LocalMockNetconfBoundary(vector)


def local_schema_validation_shadow(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return the HTTPS-schema equivalent of a loopback HTTP vector."""
    shadow = deepcopy(value)
    endpoint_slots = (
        ("r1", "apiRoot"), ("a1", "apiRoot"), ("a1", "statusCallbackRoot"),
    )
    for first, second in endpoint_slots:
        current = shadow[first][second]
        if isinstance(current, str) and current.startswith("http://"):
            shadow[first][second] = "https://" + current[len("http://"):]
    nested_slots = (
        ("r1", "callbackApi", "rootUri"),
        ("r1", "dme", "policyEvidencePushBaseUri"),
        ("o1", "fileDataReporting", "mnsRoot"),
        ("o1", "fileDataReporting", "consumerReference"),
        ("o1", "perfMetricJob", "managedObjectUri"),
    )
    for first, second, third in nested_slots:
        current = shadow[first][second][third]
        if isinstance(current, str) and current.startswith("http://"):
            shadow[first][second][third] = "https://" + current[len("http://"):]
    return shadow


def _load_local_vector(path: Path, bundle: ContractBundle) -> dict[str, Any]:
    """Validate a vector while allowing only loopback HTTP scheme relaxation."""
    value = json.loads(path.read_text(encoding="utf-8"))
    validate_json_schema(local_schema_validation_shadow(value),
                         bundle.schema("deployment-test-vector"), bundle.path)
    return value


@dataclass(frozen=True)
class _Endpoint:
    host: str
    port: int
    path: str
    origin: str


def _endpoint(uri: str, label: str) -> _Endpoint:
    parsed = urlsplit(uri)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise LocalProfileError(f"{label} must be an explicit loopback HTTP URI")
    try:
        port = parsed.port
    except ValueError as exc:
        raise LocalProfileError(f"{label} has an invalid port") from exc
    if port is None:
        raise LocalProfileError(f"{label} must declare its listen port")
    host_for_uri = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return _Endpoint(parsed.hostname, port, parsed.path.rstrip("/"), f"http://{host_for_uri}:{port}")


class _DispatchServer(ThreadingHTTPServer):
    """HTTP shell around a component's real ``dispatch`` method."""

    def __init__(self, endpoint: _Endpoint, dispatch: Callable[..., Any]):
        self.component_dispatch = dispatch
        super().__init__((endpoint.host, endpoint.port), _DispatchHandler)


class _DispatchHandler(BaseHTTPRequestHandler):
    server: _DispatchServer

    def log_message(self, _format: str, *args: Any) -> None:
        return

    def _run(self) -> None:
        parsed = urlsplit(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        body: Mapping[str, Any] | None = None
        if raw:
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                self.send_error(400)
                return
            if not isinstance(decoded, dict):
                self.send_error(400)
                return
            body = decoded
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        try:
            result = self.server.component_dispatch(
                self.command, parsed.path, body, query=query)
        except TypeError:
            result = self.server.component_dispatch(self.command, parsed.path, body)
        status = int(result.status or 503)
        payload = b"" if result.body is None else json.dumps(
            result.body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        for name, value in (result.headers or {}).items():
            self.send_header(str(name), str(value))
        if payload:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    do_GET = _run
    do_POST = _run
    do_PUT = _run
    do_DELETE = _run


class LocalMockProfile:
    """Own and supervise one instance of every local mock component."""

    def __init__(self, integration_values_path: str | Path, state_dir: str | Path,
                 contract_authority: str | Path | None = None):
        self.integration_path = Path(integration_values_path).resolve()
        self.state_dir = Path(state_dir).resolve()
        document = load_integration_values(self.integration_path)
        self.values = dict(document["values"])
        vector_path = Path(self.values["deployment.testVectorPath"])
        if not vector_path.is_absolute():
            vector_path = self.integration_path.parent / vector_path
        raw_vector = vector_path.read_bytes()
        if hashlib.sha256(raw_vector).hexdigest() != self.values["deployment.testVectorSha256"]:
            raise LocalProfileError("deployment vector byte digest mismatch")
        bundle = ContractBundle.discover(contract_authority)
        self.vector = local_development_vector(
            _load_local_vector(vector_path, bundle), insecure_dev_loopback=True)
        # The frozen integration-values schema deliberately requires HTTPS.
        # The deployment vector is the contract-defined source for loopback
        # development endpoints, so only endpoint keys are overlaid here.
        self.runtime_values = dict(self.values)
        self.runtime_values.update({
            "r1.apiRoot": self.vector["r1"]["apiRoot"],
            "a1.apiRoot": self.vector["a1"]["apiRoot"],
            "a1.notificationDestination": self.vector["a1"]["statusCallbackRoot"],
            "r1.dme.policyEvidencePushBaseUri": self.vector["r1"]["dme"]["policyEvidencePushBaseUri"],
            "o1.fileDataReporting.mnsRoot": self.vector["o1"]["fileDataReporting"]["mnsRoot"],
            "o1.fileDataReporting.consumerReference": self.vector["o1"]["fileDataReporting"]["consumerReference"],
        })
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._servers: list[Any] = []
        self._threads: list[threading.Thread] = []
        self._build(bundle)

    def _build(self, bundle: ContractBundle) -> None:
        a1 = _endpoint(self.runtime_values["a1.apiRoot"], "a1.apiRoot")
        r1 = _endpoint(self.runtime_values["r1.apiRoot"], "r1.apiRoot")
        o1_provider_ep = _endpoint(
            self.runtime_values["o1.fileDataReporting.mnsRoot"], "o1.fileDataReporting.mnsRoot")
        o1_consumer_ep = _endpoint(
            self.runtime_values["o1.fileDataReporting.consumerReference"],
            "o1.fileDataReporting.consumerReference")
        callback = _endpoint(
            self.vector["r1"]["callbackApi"]["rootUri"], "r1.callbackApi.rootUri")
        push = _endpoint(
            self.runtime_values["r1.dme.policyEvidencePushBaseUri"],
            "r1.dme.policyEvidencePushBaseUri")
        if callback.origin != push.origin:
            raise LocalProfileError("rApp callback and DME push roots must share one origin")

        self.nearrt = NearRtMock(
            state_path=self.state_dir / "nearrt.json",
            near_rt_ric_id=self.vector["topology"]["nearRtRicId"],
            control_drain_wait_seconds=20.0)
        self.nearrt_server = create_nearrt_server(
            self.nearrt, a1.host, a1.port, api_root=a1.path, insecure_dev=True)
        self._start_httpd(self.nearrt_server, "near-rt")

        self.nonrt = create_service_from_values(
            self.runtime_values, bundle_dir=bundle.path,
            database_path=self.state_dir / "nonrt.sqlite3",
            artifact_base_dir=self.integration_path.parent,
            listen_host=r1.host, insecure_dev_mode=True)
        self.nonrt_server = NonRtHttpServer(
            (r1.host, r1.port), self.nonrt, path_prefix=r1.path)

        subscription_path = (
            o1_provider_ep.path.rstrip("/")
            + "/FileDataReportingMnS/"
            + self.runtime_values["o1.fileDataReporting.mnsVersion"]
            + "/subscriptions"
        )
        self.o1_provider = O1ProviderMock(
            subscription_path=subscription_path, dev_loopback_insecure=True)
        self.o1_provider_server = _DispatchServer(o1_provider_ep, self.o1_provider.dispatch)

        ledger = DurableJson(self.state_dir / "o1-consumer.json")
        netconf_profile = bundle.fixture("fixture://o1NetconfYangProfile")
        rpc_paths = {
            item["requestFixture"]
            for group in (netconf_profile["lifecycle"], netconf_profile["teardown"])
            for item in group if "requestFixture" in item
        }
        rpc_bytes = {
            Path(relative).stem: (bundle.path / relative).read_bytes()
            for relative in rpc_paths
        }
        netconf = NetconfPerfMetricJobManager(
            rpc_bytes, self.o1_provider.netconf,
            netconf_profile["transport"]["requiredCapabilities"])
        subscription = SubscriptionManager(
            ledger, self.runtime_values["o1.fileDataReporting.consumerReference"],
            post=lambda body: self.o1_provider.create_subscription(body),
            delete=lambda sid: self.o1_provider.delete_subscription(sid))
        self.o1_consumer = O1Consumer(
            subscription, NotificationReceiver(ledger),
            netconf=netconf,
            dev_loopback_insecure=True,
            notification_path=(o1_consumer_ep.path.rstrip("/")
                               + "/file-data-reporting/notifications"))
        self.o1_consumer_server = _DispatchServer(
            o1_consumer_ep, self.o1_consumer.dispatch)

        capability = _load_pinned_json(
            self.integration_path, self.values["backend.capabilityManifestPath"],
            self.values["backend.capabilityManifestSha256"])
        self.r1_client = R1Client(
            api_root=self.runtime_values["r1.apiRoot"],
            r_app_id=self.vector["r1"]["rAppId"],
            policy_evidence_push_base_uri=self.runtime_values["r1.dme.policyEvidencePushBaseUri"],
            state_path=self.state_dir / "rapp-r1.json", insecure_dev=True)
        self.rapp = RAppCoordinatorAdapter(
            dispatch=self.r1_client, assurance=CombinedAssurance(capability),
            ledger=EvidenceLedger(self.state_dir / "rapp-evidence.json"),
            capability_manifest=capability,
            llm_manager=LLMBackendManager.with_backend(
                DeterministicMockBackend(seed=7)))
        self.rapp_server = RAppHttpServer(
            host=callback.host, port=callback.port, client=self.r1_client,
            coordinator=self.rapp,
            status_callback_path=(callback.path.rstrip("/") + "/r1/a1-policy-status"),
            dme_push_base_path=push.path, insecure_dev=True, enable_harness=True,
            a1_status_callback_path=_endpoint(
                self.vector["a1"]["statusCallbackRoot"],
                "a1.statusCallbackRoot").path)
        a1_callback = _endpoint(
            self.vector["a1"]["statusCallbackRoot"], "a1.statusCallbackRoot")
        if a1_callback.origin != callback.origin:
            raise LocalProfileError("A1 status callback and rApp callback must share one origin")
        def accept_a1_status(body: Mapping[str, Any]) -> int:
            validate_status(dict(body))
            return self.nonrt.handle(
                "POST", self.nonrt.a1_notification_path, body=dict(body)).status
        # The local profile models the producer-originated callback at the
        # actual Non-RT ingress.  Fault/retry outcomes are therefore emitted by
        # the producer rather than manufactured by the scenario runner.
        self.nearrt.callback = lambda body: accept_a1_status(body)
        self.rapp_server.accept_a1_status = accept_a1_status
        self.rapp_server.on_evidence_accepted = self.nonrt.record_dme_push
    def _start_httpd(self, server: Any, name: str) -> None:
        thread = threading.Thread(
            target=server.serve_forever, name=f"local-mock-{name}", daemon=True)
        thread.start()
        self._servers.append(server)
        self._threads.append(thread)

    def start(self) -> None:
        # Near-RT starts during construction so Non-RT can perform authoritative
        # A1 discovery.  The remaining surfaces start only after all validation.
        for server, name in (
                (self.nonrt_server, "non-rt"),
                (self.o1_provider_server, "o1-provider"),
                (self.o1_consumer_server, "o1-consumer")):
            self._start_httpd(server, name)
        self.rapp_server.start()

    def close(self) -> None:
        self.rapp_server.close()
        for server in reversed(self._servers):
            server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=2)
        self._servers.clear()
        self._threads.clear()

    def __enter__(self) -> "LocalMockProfile":
        self.start()
        return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the loopback O-RAN local mock profile")
    parser.add_argument("--integration-values", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument(
        "--contract-authority", type=Path,
        help="authority tree containing shared-contract-bundle (defaults to 1.0.1)")
    parser.add_argument("--insecure-dev-loopback", action="store_true", required=True)
    args = parser.parse_args(argv)
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _signum, _frame: stop.set())
    with LocalMockProfile(
            args.integration_values, args.state_dir,
            contract_authority=args.contract_authority):
        stop.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
