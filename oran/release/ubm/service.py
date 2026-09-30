"""Composition root for the upper-bilateral-mock runtime.

Four loopback TLS listeners are assembled from the *existing* upper components
- ``oran.nonrt``, ``oran.rapp``, ``oran.o1`` and ``oran.mocks.o1provider`` - so
the lower frozen runner can drive every endpoint over the network without ever
importing upper source.  This module adds no O-RAN route: it binds, terminates
TLS, gates the Integration Control Surface and records capture.

``oran.profiles.local_mock`` is deliberately *not* imported: its schema-rewrite
shadow must not appear on the bilateral import graph (gate G-TLS-1), and its
loopback-HTTP requirement is the opposite of D-3.  G-TLS-1 is enforced as a byte
grep over the packaged members, so the shadow's function name is never written
out under this package.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from decision.llm_backend import DeterministicMockBackend, LLMBackendManager
from oran.conformance.contracts import ContractBundle
from oran.contract.integration_values import load_integration_values
from oran.contract.jcs import canonicalize_bytes, jcs_sha256
from oran.mocks.o1provider.provider import O1ProviderMock
from oran.nonrt.a1_client import A1PClient
from oran.nonrt.capability import CapabilityArtifacts, validate_e2_inventory
from oran.nonrt.config import SecurityProfile
from oran.nonrt.http_server import NonRtHttpServer
from oran.nonrt.service import NonRtRicService
from oran.o1.core import (
    DurableJson, NetconfPerfMetricJobManager, NotificationReceiver, O1Consumer,
    SubscriptionManager,
)
from oran.rapp.assurance import CombinedAssurance
from oran.rapp.coordinator_adapter import RAppCoordinatorAdapter
from oran.rapp.evidence import EvidenceLedger
from oran.rapp.http_servers import RAppHttpServer
from oran.rapp.r1_client import R1Client

from .capture import (
    CaptureRecorder, LogicalClock, default_schema_path, supplied_values)
from .clients import (
    CapturingA1Transport, CapturingCallbackSender, CapturingHttpsClient,
    OutboundRecorderBinding,
)
from .config import UbmStartupConfig
from .egress import EgressGuard, load_hardware_control_surfaces
from .http import CaptureContext, DispatchServer, assert_port_free, bind_tls, capturing_handler
from .ics import IntegrationControlSurface
from .plan import (
    BILATERAL_SCENARIOS, PlanMatcher, ScenarioPlan, TemplateIndex,
    assert_root_mappings, build_plan, roots_from_vector, scenario_entry,
)
from .preflight import (
    assert_https_endpoints, assert_placeholder_free,
    assert_schema_external_invariants, authority_of, base_path_of,
    check_dependency_lock, load_vector, validate_vector_against_frozen_schema,
)
from .tls import SecretResolver, build_client_ssl_context, build_server_ssl_context

CONTROL_PREFIX = "/ubm/v1"
STATUS_CALLBACK_SUFFIX = "/r1/a1-policy-status"
CAPABILITY_DME_TYPE = "aic:ran-capability:1.0.0"
EVIDENCE_DME_TYPE = "aic:policy-evidence:1.0.0"
EVIDENCE_DELIVERY_SCHEMA_ID = "aic.policy-evidence.record.schema.1.0.0"


class ServiceStartupError(RuntimeError):
    exit_code = 78


@dataclass(frozen=True)
class UbmEndpoints:
    r1: str
    rapp: str
    o1_provider: str
    o1_consumer: str
    lower_a1: str


class UpperBilateralMockService:
    """Own and supervise exactly the four upper listeners."""

    def __init__(self, *, config: UbmStartupConfig, bundle: ContractBundle,
                 vector: Mapping[str, Any], vector_sha256: str,
                 values: Mapping[str, Any], resolver: SecretResolver,
                 dependency_versions: Mapping[str, str],
                 vector_schema_sha256: str) -> None:
        self.config = config
        self.bundle = bundle
        self.vector = dict(vector)
        self.vector_sha256 = vector_sha256
        self.vector_schema_sha256 = vector_schema_sha256
        self.values = dict(values)
        self.resolver = resolver
        self.dependency_versions = dict(dependency_versions)
        self.roots = roots_from_vector(self.vector)
        assert_root_mappings(bundle.runner, list(self.roots))
        self.templates = TemplateIndex(bundle.catalog["endpointTemplates"], self.roots)
        self._lock = threading.RLock()
        self._started = False
        self._stopped = False
        self._scenario_id: str | None = None
        self._plan: ScenarioPlan | None = None
        self._recorder: CaptureRecorder | None = None
        self._clock = LogicalClock(origin_iso="1970-01-01T00:00:00Z")
        self._release = _read_release_manifest(config.release_manifest_path)
        self._schema_path = _resolve_schema_path(config)
        self._exports: dict[str, str] = {}
        # AGREED_UPPER_OBSERVATION: r1-status-to-rapp has both ends upper, so
        # the lower double cannot see it on the wire.  These two counters are
        # the agreed ICS-visible evidence; the same exchange is also in capture
        # with peer UPPER_SELF.
        self._a1_status_callbacks_received = 0
        self._last_a1_status_callback_status: int | None = None
        self._status_callbacks_relayed = 0
        self._last_status_callback_status: int | None = None
        self._build()

    # -- construction -----------------------------------------------------
    @classmethod
    def from_startup(cls, config: UbmStartupConfig) -> "UpperBilateralMockService":
        config.validate()
        bundle = ContractBundle(Path(config.contract_authority))
        vector, digest, _ = load_vector(Path(config.vector_path))
        if digest != config.vector_sha256:
            raise ServiceStartupError("deployment vector digest drifted after load")
        schema_digest = validate_vector_against_frozen_schema(vector, bundle)
        assert_placeholder_free(vector)
        assert_https_endpoints(vector)
        assert_schema_external_invariants(vector, bundle)
        values = load_integration_values(Path(config.integration_values_path))["values"]
        dependency_versions = check_dependency_lock(config.dependency_lock_path)
        resolver = SecretResolver.from_file(Path(config.secret_map_path))
        return cls(config=config, bundle=bundle, vector=vector,
                   vector_sha256=digest, values=values, resolver=resolver,
                   dependency_versions=dependency_versions,
                   vector_schema_sha256=schema_digest)

    def _build(self) -> None:
        vector = self.vector
        r1_host, r1_port = authority_of(vector["r1"]["apiRoot"], "/r1/apiRoot")
        rapp_host, rapp_port = authority_of(
            vector["r1"]["callbackApi"]["rootUri"], "/r1/callbackApi/rootUri")
        push_host, push_port = authority_of(
            vector["r1"]["dme"]["policyEvidencePushBaseUri"],
            "/r1/dme/policyEvidencePushBaseUri")
        status_host, status_port = authority_of(
            vector["a1"]["statusCallbackRoot"], "/a1/statusCallbackRoot")
        if (rapp_host, rapp_port) != (push_host, push_port) or \
                (rapp_host, rapp_port) != (status_host, status_port):
            raise ServiceStartupError(
                "the rApp callback, DME push and A1 status roots must share one "
                "origin; the frozen binding document assigns them to L2-rapp")
        provider_host, provider_port = authority_of(
            vector["o1"]["fileDataReporting"]["mnsRoot"], "/o1/fileDataReporting/mnsRoot")
        consumer_host, consumer_port = authority_of(
            vector["o1"]["fileDataReporting"]["consumerReference"],
            "/o1/fileDataReporting/consumerReference")
        a1_host, a1_port = authority_of(vector["a1"]["apiRoot"], "/a1/apiRoot")
        if (a1_host, a1_port) in {(r1_host, r1_port), (rapp_host, rapp_port),
                                  (provider_host, provider_port),
                                  (consumer_host, consumer_port)}:
            raise ServiceStartupError(
                "a1.apiRoot is lower-owned and must not collide with an upper listener")

        self._endpoints = UbmEndpoints(
            r1="https://%s:%d" % (r1_host, r1_port),
            rapp="https://%s:%d" % (rapp_host, rapp_port),
            o1_provider="https://%s:%d" % (provider_host, provider_port),
            o1_consumer="https://%s:%d" % (consumer_host, consumer_port),
            lower_a1="https://%s:%d" % (a1_host, a1_port))
        self._authorities = tuple(
            "%s:%d" % pair for pair in (
                (r1_host, r1_port), (rapp_host, rapp_port),
                (provider_host, provider_port), (consumer_host, consumer_port),
                (a1_host, a1_port)))

        for host, port in ((r1_host, r1_port), (rapp_host, rapp_port),
                           (provider_host, provider_port),
                           (consumer_host, consumer_port)):
            assert_port_free(host, port)

        state_dir = Path(self.config.state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        timeout_ms = int(self.vector["timeouts"]["defaultStepMs"])
        self._timeout_ms = timeout_ms

        client_context = build_client_ssl_context(
            truststore_ref=self.values["r1.https.truststoreRef"],
            resolver=self.resolver)
        https_client = CapturingHttpsClient(context=client_context,
                                            timeout_ms=timeout_ms)
        self._outbound = OutboundRecorderBinding()

        # -- Non-RT RIC framework (L1) ------------------------------------
        capability = self.bundle.fixture("fixture://capabilityManifest")
        artifacts = self._capability_artifacts(capability)
        a1_client = A1PClient(CapturingA1Transport(
            api_root=self._endpoints.lower_a1, client=https_client,
            binding=self._outbound))
        rapp_identity = str(self.vector["r1"]["rAppId"])
        security = SecurityProfile(
            insecure_dev_mode=False,
            listen_host=r1_host,
            # The deployment binds exactly one rApp identity to this loopback
            # listener; there is no contract-declared identity header for it.
            auth_hook=lambda _headers, identity=rapp_identity: identity,
            integration_control_approved=self.config.integration_control_approved)
        self.callback_sender = CapturingCallbackSender(
            client=https_client, binding=self._outbound,
            observer=self._note_status_callback)
        self.nonrt = NonRtRicService(
            database_path=state_dir / "nonrt.sqlite3",
            a1_client=a1_client,
            capability_manifest=capability,
            a1_ready=artifacts.a1_ready,
            capability_artifacts=artifacts,
            bundle_dir=self.bundle.path,
            security=security,
            r1_api_root=self._endpoints.r1 + base_path_of(vector["r1"]["apiRoot"]),
            a1_notification_destination=vector["a1"]["statusCallbackRoot"],
            bootstrap_info={"bootstrapInformation": []},
            callback_sender=self.callback_sender,
            capability_registration=self._capability_registration(capability),
            # R-1 disposition (see DESIGN.md 13): the pre-PUT A1 reconciliation
            # probe is a GET of #/endpointTemplates/a1Policies that no bilateral
            # scenario declares.  scenario-runner-contract.1.0.1.json
            # #/resultCollection/httpSequence counts "each actual HTTP request"
            # and requires the vector to equal expected.httpSequence exactly,
            # and all four bilateral scenarios declare `faults: []`, so the
            # probe is neither required nor tolerated on this profile.  It is
            # disabled here only; every offline caller keeps the default.
            a1_pre_put_reconciliation_probe=False)
        self.nonrt_server = NonRtHttpServer(
            (r1_host, r1_port), self.nonrt,
            path_prefix=base_path_of(vector["r1"]["apiRoot"]))

        # -- rApp callback / DME push / A1 status (L2) --------------------
        self.r1_client = R1Client(
            api_root=self._endpoints.r1 + base_path_of(vector["r1"]["apiRoot"]),
            r_app_id=rapp_identity,
            policy_evidence_push_base_uri=vector["r1"]["dme"]["policyEvidencePushBaseUri"],
            state_path=state_dir / "rapp-r1.json",
            timeout_s=timeout_ms / 1000.0,
            max_attempts=1, retry_backoff_s=(),
            ssl_context=client_context,
            oauth_header_provider=self._oauth_header_provider())
        self.coordinator = RAppCoordinatorAdapter(
            dispatch=self.r1_client, assurance=CombinedAssurance(capability),
            ledger=EvidenceLedger(state_dir / "rapp-evidence.json"),
            capability_manifest=capability,
            # Seeded, offline and deterministic: the bilateral profile must
            # never reach a remote model, and two runs must agree.
            llm_manager=LLMBackendManager.with_backend(
                DeterministicMockBackend(seed=7)))
        self.rapp_server = RAppHttpServer(
            host=rapp_host, port=rapp_port, client=self.r1_client,
            coordinator=self.coordinator,
            status_callback_path=base_path_of(
                vector["r1"]["callbackApi"]["rootUri"]) + STATUS_CALLBACK_SUFFIX,
            dme_push_base_path=base_path_of(
                vector["r1"]["dme"]["policyEvidencePushBaseUri"]),
            insecure_dev=False,
            enable_harness=True,
            integration_control_approved=self.config.integration_control_approved,
            ssl_context=self._server_context(
                "a1.https.notificationReceiverServerCertificateRef",
                "a1.https.notificationReceiverServerPrivateKeyRef"),
            # Loopback TLS is the only authorization boundary declared for the
            # bilateral callback listener; no credential header is contract-declared.
            authorize=lambda _headers: True,
            a1_status_callback_path=base_path_of(vector["a1"]["statusCallbackRoot"]),
            accept_a1_status=self._accept_a1_status,
            on_evidence_accepted=self.nonrt.record_dme_push)

        # -- O1 provider (L3) and O1 consumer (L4) ------------------------
        subscription_path = (
            base_path_of(vector["o1"]["fileDataReporting"]["mnsRoot"])
            + "/FileDataReportingMnS/"
            + str(vector["o1"]["fileDataReporting"]["mnsVersion"])
            + "/subscriptions")
        self.o1_provider = O1ProviderMock(
            subscription_path=subscription_path, dev_loopback_insecure=True)
        self.o1_provider_server = DispatchServer(
            (provider_host, provider_port), self.o1_provider.dispatch)

        ledger = DurableJson(state_dir / "o1-consumer.json")
        subscription = SubscriptionManager(
            ledger, vector["o1"]["fileDataReporting"]["consumerReference"],
            post=lambda body: self.o1_provider.create_subscription(body),
            delete=lambda sid: self.o1_provider.delete_subscription(sid))
        # SC-083's SET_PERF_METRIC_JOB_UNLOCKED initial state drives the O1
        # consumer's NETCONF boundary, so the manager is wired from the bundle's
        # own YANG profile and RPC fixtures - no live NETCONF endpoint.
        netconf_profile = self.bundle.fixture("fixture://o1NetconfYangProfile")
        rpc_paths = {
            item["requestFixture"]
            for group in (netconf_profile["lifecycle"], netconf_profile["teardown"])
            for item in group if "requestFixture" in item
        }
        netconf = NetconfPerfMetricJobManager(
            {Path(relative).stem: (self.bundle.path / relative).read_bytes()
             for relative in rpc_paths},
            self.o1_provider.netconf,
            netconf_profile["transport"]["requiredCapabilities"])
        # The catalog template ``o1NotificationRecipient`` is
        # ``{o1ConsumerRoot}/file-data-reporting/notifications`` and the vector's
        # consumerReference already carries a path, so the contract-expanded
        # target is the root path plus that suffix.  The bare root path is the
        # vector's own declared consumer reference; both are routed to the one
        # receiver so the upper answers whichever the lower runner expands.
        consumer_base = base_path_of(
            vector["o1"]["fileDataReporting"]["consumerReference"])
        self._o1_notification_path = consumer_base + "/file-data-reporting/notifications"
        self.o1_consumer = O1Consumer(
            subscription, NotificationReceiver(ledger),
            netconf=netconf,
            dev_loopback_insecure=True,
            notification_path=self._o1_notification_path)
        self._o1_notification_alias = consumer_base or "/"
        self.o1_consumer_server = DispatchServer(
            (consumer_host, consumer_port), self._o1_consumer_dispatch)

        # The egress guard turns the capture's external-call counters from
        # literals into observations.  Its allowlist is exactly the authority
        # set this deployment declares -- the four upper listeners plus the
        # lower A1 origin, all derived from the vector above -- and its
        # hardware definition comes from the frozen release-gates bytes.
        hardware, hardware_source = load_hardware_control_surfaces(
            self._gates_path())
        self.egress = EgressGuard(allowlist=self._authorities, hardware=hardware,
                                  hardware_source=hardware_source)

        # -- capture contexts and control planes --------------------------
        self.contexts = {
            "nonrt": CaptureContext(
                component="nonrt", base_uri=self._endpoints.r1,
                authority="%s:%d" % (r1_host, r1_port), timeout_ms=timeout_ms,
                templates=self.templates),
            "rapp": CaptureContext(
                component="rapp", base_uri=self._endpoints.rapp,
                authority="%s:%d" % (rapp_host, rapp_port), timeout_ms=timeout_ms,
                templates=self.templates,
                # Row 4 of SC-083 is upper->upper; it is captured once, on the
                # client side, so the server side of that one path is not
                # recorded a second time.
                suppressed_capture_paths=(
                    base_path_of(vector["r1"]["callbackApi"]["rootUri"])
                    + STATUS_CALLBACK_SUFFIX,)),
            "o1_provider": CaptureContext(
                component="o1_provider", base_uri=self._endpoints.o1_provider,
                authority="%s:%d" % (provider_host, provider_port),
                timeout_ms=timeout_ms, templates=self.templates),
            "o1_consumer": CaptureContext(
                component="o1_consumer", base_uri=self._endpoints.o1_consumer,
                authority="%s:%d" % (consumer_host, consumer_port),
                timeout_ms=timeout_ms, templates=self.templates),
        }
        for name, context in self.contexts.items():
            context.control = self._control_plane
            context.egress = self.egress
            if self.config.integration_control_approved:
                context.ics = IntegrationControlSurface(
                    component=name, delegate=self._delegate_for(name),
                    recorder=_RecorderProxy(self), approved=True,
                    host=_host_of(context.authority))

        bind_tls(self.nonrt_server, self._server_context(
            "r1.https.pushServerCertificateRef",
            "r1.https.pushServerPrivateKeyRef"))
        bind_tls(self.o1_provider_server, self._server_context(
            "o1.https.providerServerCertificateRef",
            "o1.https.providerServerPrivateKeyRef"))
        bind_tls(self.o1_consumer_server, self._server_context(
            "o1.https.notificationReceiverServerCertificateRef",
            "o1.https.notificationReceiverServerPrivateKeyRef"))

        self.nonrt_server.RequestHandlerClass = capturing_handler(
            self.nonrt_server.RequestHandlerClass, self.contexts["nonrt"])
        self.o1_provider_server.RequestHandlerClass = capturing_handler(
            self.o1_provider_server.RequestHandlerClass,
            self.contexts["o1_provider"])
        self.o1_consumer_server.RequestHandlerClass = capturing_handler(
            self.o1_consumer_server.RequestHandlerClass,
            self.contexts["o1_consumer"])
        self.rapp_server.httpd.RequestHandlerClass = capturing_handler(
            self.rapp_server.httpd.RequestHandlerClass, self.contexts["rapp"])

        self._threads: list[threading.Thread] = []

    # -- helpers ----------------------------------------------------------
    def _supplied_values(self) -> frozenset[str]:
        """Deployment-supplied identifiers, read from the frozen bytes."""
        provenance = None
        candidate = Path(self.config.spec_dir) / "binding-provenance.1.0.0.json"
        if candidate.is_file():
            provenance = json.loads(candidate.read_text(encoding="utf-8"))
        return supplied_values(self.vector, self.bundle.catalog.get("constants"),
                               provenance)

    def _gates_path(self) -> Path | None:
        candidate = Path(self.config.spec_dir) / "release-gates.1.0.0.json"
        if candidate.is_file():
            return candidate
        fallback = (Path(__file__).resolve().parents[3] / "docs"
                    / "upper-bilateral-mock" / "release-gates.1.0.0.json")
        return fallback if fallback.is_file() else None

    def _server_context(self, certificate_key: str, private_key_key: str) -> Any:
        certificate_ref = self.values[certificate_key]
        private_key_ref = self.values[private_key_key]
        return build_server_ssl_context(
            certificate_ref=certificate_ref, private_key_ref=private_key_ref,
            truststore_ref=self.values.get("r1.https.truststoreRef"),
            resolver=self.resolver)

    def _oauth_header_provider(self) -> Callable[[str, str, str], str]:
        reference = self.values["r1.https.oauthCredentialRef"]
        scheme = "Bearer"

        def provide(_method: str, _url: str, _version: str) -> str:
            token = self.resolver.resolve_path(reference).read_text(
                encoding="utf-8").strip()
            return scheme + " " + token

        return provide

    def _o1_consumer_dispatch(self, method: str, path: str,
                              body: Any = None, **kwargs: Any) -> Any:
        """Route the vector's bare consumer reference to the same receiver."""
        del kwargs
        if path.rstrip("/") == self._o1_notification_alias.rstrip("/"):
            path = self._o1_notification_path
        return self.o1_consumer.dispatch(method, path, body)

    def _note_status_callback(self, status: int) -> None:
        with self._lock:
            self._status_callbacks_relayed += 1
            self._last_status_callback_status = int(status)

    def _accept_a1_status(self, body: Mapping[str, Any]) -> int:
        status = self.nonrt.handle(
            "POST", self.nonrt.a1_notification_path,
            body=self._correlate_a1_status(body)).status
        with self._lock:
            self._a1_status_callbacks_received += 1
            self._last_a1_status_callback_status = int(status)
        return status

    def _correlate_a1_status(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Bind a producer-originated status to the upper's assigned policyId.

        The reference runner substitutes the server-assigned identifier into the
        emitted snapshot (``oran/conformance/runner.py:1578-1585``), in which
        case this is a no-op.  If a runner instead emits the catalog fixture
        verbatim, the snapshot names a policy this framework never assigned and
        the framework would answer 404.  The status carries
        ``aicStatus.trace.intentId``/``correlationId``, and the policy object
        carries the same trace, so the correlation key is contract-declared: it
        is used only when it identifies exactly one stored policy, and the
        stored bytes are then identical to the substituting-runner case.
        """
        document = json.loads(json.dumps(dict(body)))
        aic = document.get("aicStatus")
        if not isinstance(aic, Mapping):
            return document
        notified = aic.get("policyId")
        if isinstance(notified, str) and self.nonrt.store.row(
                "SELECT 1 FROM policies WHERE policy_id=?", (notified,)) is not None:
            return document
        trace = aic.get("trace")
        if not isinstance(trace, Mapping):
            return document
        matches = [
            str(row["policy_id"])
            for row in self.nonrt.store.rows("SELECT policy_id,policy_json FROM policies")
            if _trace_matches(self.nonrt.store.decode(row["policy_json"]), trace)
        ]
        if len(matches) == 1:
            document["aicStatus"]["policyId"] = matches[0]
        return document

    def _capability_artifacts(self, capability: Mapping[str, Any]
                              ) -> CapabilityArtifacts:
        """Derive the E2 inventory from the same fixture the contract pins.

        SC-083's R1 create body binds ``fixture://capabilityManifest#/nearRtRicId``,
        so the bundle fixture is the only admissible capability source (V-2).
        The deployment vector's ``e2Inventory`` is bound to a *different*
        capability manifest (32-bit node ids versus the fixture's 16-bit), so it
        cannot be validated against the fixture; only its declared ``status`` is
        consumed.  Readiness publishes both digests and the derivation.
        """
        declared = dict(self.vector["e2Inventory"])
        nodes = list(capability["e2Deployment"]["nodes"])
        connections = list(declared["connections"])
        if len(connections) != len(nodes):
            raise ServiceStartupError(
                "the deployment vector declares %d E2 connections but the pinned "
                "capability declares %d nodes" % (len(connections), len(nodes)))
        by_key = {_node_key(node["globalE2NodeId"]["nodeId"]): node for node in nodes}
        inventory = {
            "schemaVersion": declared["schemaVersion"],
            "contractProfile": declared["contractProfile"],
            "generatedAt": declared["generatedAt"],
            "releaseManifestSha256": declared["releaseManifestSha256"],
            "status": declared["status"],
            "connections": [
                _derive_connection(
                    connection,
                    by_key[_node_key(connection["globalE2NodeId"]["nodeId"])],
                    capability)
                for connection in connections],
        }
        result = validate_e2_inventory(inventory, capability,
                                       bundle_dir=self.bundle.path)
        self._e2_inventory = inventory
        self._e2_inventory_declared_status = str(declared["status"])
        return CapabilityArtifacts(
            capability_manifest=dict(capability),
            release_manifest={},
            e2_inventory=inventory,
            capability_sha256=hashlib.sha256(
                canonicalize_bytes(capability)).hexdigest(),
            release_sha256="",
            inventory_sha256=hashlib.sha256(
                canonicalize_bytes(inventory)).hexdigest(),
            inventory_result=result)

    def _capability_registration(self, capability: Mapping[str, Any]
                                 ) -> dict[str, Any]:
        schema = self.bundle.schema("aic.ran-capability.1.0.0.schema.json")
        return {
            "dmeTypeDefinition": {
                "dmeTypeId": CAPABILITY_DME_TYPE,
                "metadata": {"dataCategory": ["PERFORMANCE"]},
                "dataProductionSchema": schema,
                "dataDeliverySchemas": [{
                    "type": "JSON_SCHEMA",
                    "deliverySchemaId": "aic.ran-capability.1.0.0",
                    "schema": canonicalize_bytes(schema).decode("utf-8"),
                }],
                "dataDeliveryMechanisms": [{"dataDeliveryMethod": "PUSH_HTTP"}],
            },
            "dataAccessEndpoint": dict(self.vector["r1"]["dme"]["dataAccessEndpoint"]),
            "dataDeliveryModes": ["CONTINUOUS"],
        }

    # -- Integration Control Surface delegates ----------------------------
    def _delegate_for(self, component: str
                      ) -> Callable[[str, Mapping[str, Any]], Mapping[str, Any]]:
        def _dispatch(op: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            if op == "RESET_SCENARIO_STATE":
                self._scenario_reset_from_ics(component, arguments)
            return self._component_delegate(component)(op, arguments)

        return _dispatch

    def _component_delegate(self, component: str
                            ) -> Callable[[str, Mapping[str, Any]], Mapping[str, Any]]:
        if component == "nonrt":
            return self._nonrt_delegate
        if component == "rapp":
            return self._rapp_delegate
        if component == "o1_consumer":
            return self._o1_consumer_delegate
        return self._o1_provider_delegate

    def _scenario_reset_from_ics(self, component: str,
                                 arguments: Mapping[str, Any]) -> None:
        """Bind the scenario when the counterpart resets through the ICS.

        A lower runner drives the profile entirely through the declared control
        surface; it never calls ``/ubm/v1/reset``.  ``RESET_SCENARIO_STATE``
        carries ``scenarioId``, so that is where the scenario binding, the plan
        and a fresh capture recorder are established.  The counterpart resets
        every component in turn, so the rebuild happens on the transition only:
        the remaining resets in the same round keep the recorder that is already
        recording them, and a repeat of a scenario that has already produced an
        exchange rebuilds again rather than appending to a dirty document.
        """
        scenario_id = str(arguments.get("scenarioId") or "")
        if not scenario_id:
            return
        recorder = self._recorder
        dirty = recorder is not None and bool(recorder.snapshot().get("exchanges"))
        if scenario_id == self._scenario_id and not dirty:
            return
        self.reset(scenario_id=scenario_id)
        fresh = self._recorder
        if fresh is not None:
            # The surface audits before the delegate runs, so this reset's own
            # audit went to the recorder that was just replaced.  Re-emit it
            # into the new document rather than losing it.
            fresh.emit_harness_operation({
                "sequence": fresh.next_sequence(),
                "logicalTimeMs": fresh.clock.now_ms(),
                "component": component,
                "op": "RESET_SCENARIO_STATE",
                "allowed": True,
                "argumentKeys": sorted(str(key) for key in arguments),
                "httpStatus": 200,
                "event": "ICS_OP",
            })

    def _nonrt_delegate(self, op: str, arguments: Mapping[str, Any]
                        ) -> Mapping[str, Any]:
        if op == "__STATE__":
            return self.nonrt.handle("GET", "/harness/state").body or {}
        arguments = dict(arguments)
        if op == "INSTALL_DME_REGISTRATION" and "registration" not in arguments:
            # Declared vocabulary: the counterpart names the registration by its
            # contract descriptors instead of shipping the object.  The object
            # is rebuilt from this release's own frozen bundle and vector, and
            # every descriptor the counterpart declared must match what was
            # rebuilt -- a mismatch fails closed rather than being ignored.
            arguments["registration"] = self._registration_for(arguments)
        response = self.nonrt.handle(
            "POST", "/harness/op", body={"op": op, **arguments})
        if response.status >= 400:
            raise ServiceStartupError("harness operation %s failed" % op)
        if op == "INSTALL_R1_STATUS_SUBSCRIPTION":
            # The Non-RT harness action is a no-op in the component; the pinned
            # statusSubscriptionId that SC-083 asserts has to exist as a durable
            # subscription row, written exactly as _create_policy's sibling does.
            self._install_status_subscription()
        return dict(response.body or {}).get("outputs", {})

    def _install_status_subscription(self) -> None:
        subscription_id = str(self.vector["r1"]["statusSubscriptionId"])
        destination = (self.vector["r1"]["callbackApi"]["rootUri"].rstrip("/")
                       + STATUS_CALLBACK_SUFFIX)
        body = {"notificationDestination": destination}
        self.nonrt.store.execute(
            "INSERT INTO subscriptions(subscription_id,body_json) VALUES(?,?) "
            "ON CONFLICT(subscription_id) DO UPDATE SET body_json=excluded.body_json",
            (subscription_id, self.nonrt.store.encode(body)))

    def _rapp_delegate(self, op: str, arguments: Mapping[str, Any]
                       ) -> Mapping[str, Any]:
        if op == "__STATE__":
            transitions = self.coordinator.transition_snapshot()
            terminal = [item for item in transitions if item["to"] == "S6"]
            return {
                "transitions": transitions,
                "coordinatorState": (terminal[-1]["to"] if terminal else
                                     transitions[-1]["to"] if transitions else "S0"),
                "terminalOutcomeCount": len(terminal),
                "upperOutcome": terminal[-1]["outcome"] if terminal else None,
                # AGREED_UPPER_OBSERVATION for the upper-to-upper R1 status
                # relay that no lower observer can see on the wire.
                "statusCallbacksReceived": self._status_callbacks_relayed,
                "lastStatusCallbackStatus": self._last_status_callback_status,
                "a1StatusCallbacksReceived": self._a1_status_callbacks_received,
                "lastA1StatusCallbackStatus": self._last_a1_status_callback_status,
                **self.coordinator.harness_observations(),
            }
        if op == "RESET_SCENARIO_STATE":
            self.coordinator.harness_reset()
            self.r1_client.harness_reset()
            return {"resetCompleted": True}
        if op == "R1_DME_BINDING_PRECREATE":
            self.r1_client.harness_precreate_binding(
                str(arguments["deliveryBindingId"]), arguments["job"])
            return {}
        if op == "R1_DME_BINDING_COMMIT":
            self.r1_client.harness_commit_binding(
                str(arguments["deliveryBindingId"]), str(arguments["dataJobId"]))
            return {}
        if op == "COORDINATOR_PROCESS_INTENT":
            return self._process_intent(arguments)
        raise ServiceStartupError("rApp operation %s is not allowlisted" % op)

    def _process_intent(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        from datetime import datetime

        from oran.rapp.headless import _context, _typed_intent

        before = self.coordinator.harness_observations()["processIntentCalls"]
        instant = datetime.fromisoformat(
            str(arguments["now"]).replace("Z", "+00:00"))
        records = json.loads(json.dumps(arguments["evidenceRecords"]))
        for record in records:
            record["correlation"]["policyId"] = arguments["policyId"]
        result = self.coordinator.process_intent(
            _typed_intent(arguments["intent"]),
            context=_context(arguments["policyContext"]),
            evidence_records=records,
            now=instant,
            identifiers=arguments["identifiers"])
        observed = self.coordinator.harness_observations()
        recorder = self._recorder
        if recorder is not None:
            recorder.clock.set_now_ms(
                max(0, recorder.clock.offset_of(str(arguments["now"]))))
            history = [
                {"from": item["from"], "to": item["to"],
                 "outcome": item.get("outcome"), "origin": "REAL"}
                for item in observed["coordinatorFsmHistory"]]
            terminal = [item for item in history if item["to"] == "S6"]
            recorder.set_coordinator({
                "processIntentCallsBefore": before,
                "processIntentCallsAfter": observed["processIntentCalls"],
                "processIntentCallsDelta":
                    observed["processIntentCalls"] - before,
                "executionMode": "REAL_PROCESS_INTENT",
                "fsmHistory": history,
                "terminalOutcome": result.get("terminal_outcome"),
                "terminalOutcomeCount": len(terminal),
                "terminalEvidenceRef": result.get("evidence_record_id"),
                "ledgerReferences": list(
                    observed["coordinatorLedgerReferences"]),
            })
        cycles = result.get("cycles") or []
        last_cycle = cycles[-1] if cycles else {}
        return {
            "processIntentCalls": observed["processIntentCalls"],
            "fsmHistory": observed["coordinatorFsmHistory"],
            "terminalOutcome": result.get("terminal_outcome"),
            "terminalEvidenceRef": result.get("evidence_record_id"),
            "ledgerReferences": observed["coordinatorLedgerReferences"],
            "profileError": last_cycle.get("profile_error"),
            "routedTo": last_cycle.get("routed_to"),
        }

    def _resolve_fixture_reference(self, arguments: Mapping[str, Any], *,
                                   reference_key: str, digest_key: str) -> Any:
        """Resolve a declared ``fixture://`` reference out of *this* bundle.

        The counterpart names a fixture and pins its RFC 8785 digest; it never
        ships the object.  That is what keeps G-CAP-1 true end to end: the only
        capability manifest that can enter the upper is the one already inside
        the release's own frozen bundle, and a digest that does not match the
        resolved fixture fails closed instead of being trusted.
        """
        reference = arguments.get(reference_key)
        if not isinstance(reference, str) or not reference.startswith("fixture://"):
            raise ServiceStartupError(
                "%s must name a fixture:// reference" % reference_key)
        resolved = self.bundle.fixture(reference)
        declared = arguments.get(digest_key)
        if declared is not None and jcs_sha256(resolved) != str(declared):
            raise ServiceStartupError(
                "%s does not match the JCS digest of %s in this release's bundle"
                % (digest_key, reference))
        return resolved

    def _o1_consumer_delegate(self, op: str, arguments: Mapping[str, Any]
                              ) -> Mapping[str, Any]:
        if op == "__STATE__":
            return self.o1_consumer.harness_state()
        arguments = dict(arguments)
        if op == "LOAD_CAPABILITY":
            # Declared vocabulary (integration-control-surface.1.0.0.json
            # #/operationArguments): manifestRef + manifestJcsSha256.  The
            # runtime's own seed path passes the object directly; both end at
            # the same G-CAP-1 assertion.
            if "capability" not in arguments:
                arguments["capability"] = self._resolve_fixture_reference(
                    arguments, reference_key="manifestRef",
                    digest_key="manifestJcsSha256")
            self._assert_bundle_capability(arguments.get("capability"))
        elif op == "LOAD_O1_PROFILE" and "capability" not in arguments:
            arguments["capability"] = self.bundle.fixture(
                "fixture://capabilityManifest")
        elif op == "O1_NORMALIZE" and "artifactBase64" not in arguments:
            arguments = self._normalization_context(arguments)
        return self.o1_consumer.harness_op(
            {"op": op, **arguments}).get("outputs", {})

    def _normalization_context(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Build the O1 normalization context from upper-side state only.

        Declared vocabulary: the counterpart names the retrieved artifact by its
        ``fixture://`` reference and its raw-byte digest.  Everything else --
        the correlation, the policy scope, the file metadata and the evaluation
        instant -- is what *this* side already knows: the identifier it assigned,
        the status snapshot it consumed, the policy object it forwarded and the
        scenario plan's logical clock.  The artifact is resolved from this
        release's own bundle and its digest must match, so a counterpart cannot
        substitute bytes; and nothing here reads the scenario's assertion
        material, so the oracle never proves itself.
        """
        import base64

        reference = str(arguments.get("artifactRef", ""))
        raw = self.bundle.fixture(reference) if reference else None
        if not isinstance(raw, (bytes, bytearray)) or not raw:
            raise ServiceStartupError(
                "O1_NORMALIZE needs a fixture:// artifactRef addressing raw bytes")
        raw = bytes(raw)
        declared_digest = arguments.get("byteSha256")
        observed_digest = hashlib.sha256(raw).hexdigest()
        if declared_digest is not None and str(declared_digest) != observed_digest:
            raise ServiceStartupError(
                "the declared O1 artifact digest does not match %s in this "
                "release's bundle" % reference)

        context = self.bundle.fixture("fixture://afterEvidence")
        correlation = json.loads(json.dumps(context["correlation"]))
        policy_scope = json.loads(json.dumps(context["policyScope"]))
        rows = self.nonrt.store.rows("SELECT * FROM policies")
        if rows:
            row = rows[-1]
            correlation["policyId"] = str(row["policy_id"])
            policy_object = self.nonrt.store.decode(row["policy_json"])
            if isinstance(policy_object, Mapping) and policy_object.get("scope"):
                policy_scope = json.loads(json.dumps(policy_object["scope"]))
            snapshot = self.nonrt.store.row(
                "SELECT status_json FROM statuses WHERE policy_id=?",
                (row["policy_id"],))
            if snapshot is not None:
                aic = self.nonrt.store.decode(snapshot["status_json"]).get("aicStatus", {})
                control = aic.get("control", {}) if isinstance(aic, Mapping) else {}
                for key, value in (("policyRevision", aic.get("policyRevision")),
                                   ("episodeId", aic.get("episodeId")),
                                   ("transactionId", control.get("transactionId")),
                                   ("actionId", control.get("actionId"))):
                    if value is not None:
                        correlation[key] = value
        source = context["source"]
        plan = self._plan
        return {
            "artifactBase64": base64.b64encode(raw).decode("ascii"),
            "correlation": correlation,
            "policyScope": policy_scope,
            "fileInfo": {
                "name": source["file"]["name"],
                "readyAt": source["file"]["readyAt"],
                "retrievedAt": source["file"]["retrievedAt"],
                "jobId": source["perfMetricJobId"],
            },
            "evaluationNow": plan.evaluation_now if plan is not None else None,
        }

    def _o1_provider_delegate(self, op: str, arguments: Mapping[str, Any]
                              ) -> Mapping[str, Any]:
        if op == "__STATE__":
            return self.o1_provider.harness_state()
        return self.o1_provider.harness_op(
            {"op": op, **dict(arguments)}).get("outputs", {})

    def _assert_bundle_capability(self, candidate: Any) -> None:
        """G-CAP-1: only the bundle fixture may be loaded as capability."""
        expected = jcs_sha256(self.bundle.fixture("fixture://capabilityManifest"))
        if candidate is None or jcs_sha256(candidate) != expected:
            raise ServiceStartupError(
                "LOAD_CAPABILITY accepts only the bundle fixture capability "
                "manifest; the lower release advertisement is not a scenario input")

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            # Armed before the first listener accepts anything, so no request
            # can be served while outbound traffic is unobserved.
            self.egress.install()
            for server, name in ((self.nonrt_server, "nonrt"),
                                 (self.o1_provider_server, "o1-provider"),
                                 (self.o1_consumer_server, "o1-consumer")):
                thread = threading.Thread(target=server.serve_forever,
                                          name="ubm-" + name, daemon=True)
                thread.start()
                self._threads.append(thread)
            self.rapp_server.start()
            self._started = True

    def readiness(self) -> dict[str, Any]:
        components = {
            "nonrt": self._started,
            "rapp": self._started,
            "o1_provider": self._started,
            "o1_consumer": self._started,
        }
        return {
            "ready": all(components.values()) and not self._stopped,
            "components": components,
            "vectorSha256": self.vector_sha256,
            "releaseContentSha256": self._release["releaseContentSha256"],
            "placeholderFree": True,
            "profile": self.config.profile,
            "endpoints": {
                "r1ApiRoot": self._endpoints.r1,
                "rAppCallbackRoot": self._endpoints.rapp,
                "mnsRoot": self._endpoints.o1_provider,
                "o1ConsumerRoot": self._endpoints.o1_consumer,
                "lowerA1ApiRoot": self._endpoints.lower_a1,
            },
            "integrationControlSurface": (
                "APPROVED" if self.config.integration_control_approved else "ABSENT"),
            "tls": {"minimumVersion": "TLSv1_2",
                    "schemaShadow": "FORBIDDEN"},
            "egress": self.egress.observations(scope="process"),
            "capabilitySource": "fixture://capabilityManifest",
            "e2InventorySource": "BUNDLE_FIXTURE_E2_DEPLOYMENT",
            "e2InventoryDeclaredStatus": self._e2_inventory_declared_status,
            "a1Ready": bool(self.nonrt.a1_ready),
            "dependencyLock": dict(self.dependency_versions),
            "scenarioId": self._scenario_id,
            "captureSchema": Path(self._schema_path).name,
        }

    def reset(self, *, scenario_id: str) -> dict[str, Any]:
        if scenario_id not in BILATERAL_SCENARIOS:
            raise ServiceStartupError(
                "%s is not in the bilateral-mock profile" % scenario_id)
        with self._lock:
            self._reset_components()
            plan = build_plan(self.bundle.catalog, scenario_id, self.roots,
                              self.bundle.fixture)
            clock = LogicalClock(origin_iso=plan.time_origin)
            recorder = CaptureRecorder(
                run_id=self.config.run_id, scenario_id=scenario_id,
                envelope=self._envelope(plan), clock=clock)
            # One scenario, one slice: the capture's counters must not depend
            # on how many scenarios ran before it (G-DET-4).
            self.egress.begin_scenario()
            recorder.bind_egress_guard(self.egress)
            recorder.set_supplied_values(self._supplied_values())
            recorder.set_state(when="before", snapshot=self._state_snapshot())
            for reference in _declared_secret_references(self.vector, self.values):
                recorder.note_secret_reference(reference)
            self._plan, self._recorder, self._clock = plan, recorder, clock
            self._scenario_id = scenario_id
            for context in self.contexts.values():
                context.bind(recorder=recorder, matcher=PlanMatcher(plan))
            self._outbound.bind(recorder=recorder, matcher=PlanMatcher(plan),
                                templates=self.templates)
            return {"scenarioId": scenario_id, "reset": True,
                    "state": recorder.snapshot()["state"]["before"]}

    def _reset_components(self) -> None:
        self._a1_status_callbacks_received = 0
        self._last_a1_status_callback_status = None
        self._status_callbacks_relayed = 0
        self._last_status_callback_status = None
        self.nonrt.store.reset_scenario_state()
        self.coordinator.harness_reset()
        self.r1_client.harness_reset()
        self.o1_consumer.harness_op({"op": "RESET_SCENARIO_STATE"})
        self.o1_provider.harness_op({"op": "RESET_SCENARIO_STATE"})

    def seed(self, *, scenario_id: str,
             initial_state: Sequence[str]) -> dict[str, Any]:
        """Run the upper-owned adapter actions of a scenario's initial state."""
        applied: list[str] = []
        source_map = _operation_source_map()[scenario_id]
        declared = {entry["token"]: entry for entry in source_map}
        for token in initial_state:
            entry = declared.get(str(token))
            if entry is None or entry["component"] == "lower":
                continue
            component = entry["component"]
            surface = self.contexts[component].ics
            if surface is None:
                raise ServiceStartupError(
                    "seeding requires the approved integration control surface")
            status, _ = surface.handle(
                "POST", "/harness/op",
                {"op": entry["adapterAction"], **self._seed_arguments(
                    scenario_id, str(token), entry["adapterAction"])})
            if status != 200:
                raise ServiceStartupError("seed %s was refused" % token)
            applied.append(str(token))
            for fan in entry.get("fanout", []):
                fan_component, _, fan_op = str(fan).partition(":")
                fan_surface = self.contexts[fan_component].ics
                if fan_surface is None:
                    continue
                fan_status, _ = fan_surface.handle(
                    "POST", "/harness/op",
                    {"op": fan_op, **self._seed_arguments(
                        scenario_id, str(token), fan_op)})
                if fan_status != 200:
                    raise ServiceStartupError("seed fan-out %s was refused" % fan)
                applied.append("%s:%s" % (fan_component, fan_op))
        return {"scenarioId": scenario_id, "applied": applied}

    def _seed_arguments(self, scenario_id: str, token: str,
                        action: str) -> dict[str, Any]:
        arguments: dict[str, Any] = {"scenarioId": scenario_id,
                                     "initialState": token}
        dme = self.vector["r1"]["dme"]
        if action in {"LOAD_CAPABILITY", "LOAD_O1_PROFILE"}:
            arguments["capability"] = self.bundle.fixture("fixture://capabilityManifest")
        elif action == "INSTALL_DME_REGISTRATION":
            arguments["registration"] = self._evidence_registration()
        elif action == "INSTALL_ACTIVE_DATA_JOB":
            arguments.update({
                "deliveryBindingId": dme["activeDeliveryBindingId"],
                "dataJobId": dme["activeDataJobId"],
                "job": self._active_data_job()})
        elif action == "R1_DME_BINDING_PRECREATE":
            arguments.update({"deliveryBindingId": dme["activeDeliveryBindingId"],
                              "job": self._active_data_job()})
        elif action == "R1_DME_BINDING_COMMIT":
            arguments.update({"deliveryBindingId": dme["activeDeliveryBindingId"],
                              "dataJobId": dme["activeDataJobId"]})
        elif action == "INSTALL_O1_SUBSCRIPTION":
            file_data = self.vector["o1"]["fileDataReporting"]
            arguments.update({
                "subscriptionId": file_data["subscriptionId"],
                "consumerReference": file_data["consumerReference"]})
        elif action == "INSTALL_PROVIDER_SUBSCRIPTION":
            file_data = self.vector["o1"]["fileDataReporting"]
            arguments.update({
                "subscriptionId": file_data["subscriptionId"],
                "consumerReference": file_data["consumerReference"]})
        return arguments

    def _registration_for(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Rebuild a DME registration from declared descriptors, fail-closed."""
        registration = self._evidence_registration()
        definition = registration["dmeTypeDefinition"]
        declared_type = arguments.get("dmeTypeId")
        rebuilt_type = definition["dmeTypeId"]
        rendered = "%s:%s:%s" % (rebuilt_type["namespace"], rebuilt_type["name"],
                                 rebuilt_type["version"])
        if declared_type is not None and str(declared_type) not in {rendered,
                                                                    json.dumps(rebuilt_type)}:
            raise ServiceStartupError(
                "declared dmeTypeId %r is not the evidence type this release "
                "registers (%s)" % (declared_type, rendered))
        declared_schema = arguments.get("deliverySchemaId")
        if declared_schema is not None and str(declared_schema) != \
                definition["dataDeliverySchemas"][0]["deliverySchemaId"]:
            raise ServiceStartupError(
                "declared deliverySchemaId does not match this release's bundle")
        declared_method = arguments.get("dataDeliveryMethod")
        if declared_method is not None and str(declared_method) != \
                definition["dataDeliveryMechanisms"][0]["dataDeliveryMethod"]:
            raise ServiceStartupError(
                "declared dataDeliveryMethod does not match this release's bundle")
        declared_endpoint = arguments.get("dataAccessEndpoint")
        if declared_endpoint is not None and dict(declared_endpoint) != \
                registration["dataAccessEndpoint"]:
            raise ServiceStartupError(
                "declared dataAccessEndpoint does not match the deployment vector")
        return registration

    def _evidence_registration(self) -> dict[str, Any]:
        return {
            "dmeTypeDefinition": {
                "dmeTypeId": {"namespace": "aic", "name": "policy-evidence",
                              "version": "1.0.0"},
                "metadata": {"dataCategory": ["PERFORMANCE"]},
                "dataProductionSchema": self.bundle.fixture(
                    "fixture://policyEvidenceFilterSchema"),
                "dataDeliverySchemas": [{
                    "type": "JSON_SCHEMA",
                    "deliverySchemaId": EVIDENCE_DELIVERY_SCHEMA_ID,
                    "schema": canonicalize_bytes(self.bundle.schema(
                        "aic.policy-evidence.1.0.0.schema.json")).decode("utf-8"),
                }],
                "dataDeliveryMechanisms": [{"dataDeliveryMethod": "PUSH_HTTP"}],
            },
            "dataAccessEndpoint": dict(self.vector["r1"]["dme"]["dataAccessEndpoint"]),
            "dataDeliveryModes": ["CONTINUOUS"],
        }

    def _active_data_job(self) -> dict[str, Any]:
        evidence = self.bundle.fixture("fixture://afterEvidence")
        dme = self.vector["r1"]["dme"]
        capability = self.bundle.fixture("fixture://capabilityManifest")
        return {
            "dataDeliveryMode": "CONTINUOUS",
            "dmeTypeId": EVIDENCE_DME_TYPE,
            "productionJobDefinition": {
                "policyTypeId": evidence["correlation"]["policyTypeId"],
                "policyId": evidence["correlation"]["policyId"],
                "minimumPolicyRevision": evidence["correlation"]["policyRevision"],
                "nearRtRicId": capability["nearRtRicId"],
            },
            "dataDeliveryMethod": "PUSH_HTTP",
            "dataDeliverySchemaId": EVIDENCE_DELIVERY_SCHEMA_ID,
            "pushDeliveryDetailsHttp": {
                "dataPushUri": dme["policyEvidencePushBaseUri"].rstrip("/")
                + "/" + str(dme["activeDeliveryBindingId"]),
            },
        }

    def export(self, out_dir: Path) -> dict[str, str]:
        with self._lock:
            recorder = self._recorder
            if recorder is None:
                raise ServiceStartupError("no scenario has been reset yet")
            recorder.set_state(when="after", snapshot=self._state_snapshot())
            self._record_simulated_writes(recorder)
            self._record_applied_rules(recorder)
            target = Path(out_dir) / ("capture-%s-%s.json" % (
                recorder.run_id, recorder.scenario_id))
            digest = recorder.export(target)
            self._exports[recorder.scenario_id] = digest
            return {"scenarioId": recorder.scenario_id,
                    "capturePath": str(target), "captureSha256": digest}

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            started = self._started
            self._started = False
            try:
                # ``BaseServer.shutdown`` blocks forever unless ``serve_forever``
                # is running, so a never-started listener is only closed.
                if started:
                    self.rapp_server.close()
                else:
                    self.rapp_server.httpd.server_close()
            finally:
                for server in (self.o1_consumer_server, self.o1_provider_server,
                               self.nonrt_server):
                    if started:
                        server.shutdown()
                    server.server_close()
                for thread in self._threads:
                    thread.join(timeout=2.0)
                self._threads.clear()
                # Disarmed last: the guard must outlive every listener so a
                # connection attempted during shutdown is still observed.
                self.egress.uninstall()

    @property
    def endpoints(self) -> UbmEndpoints:
        return self._endpoints

    @property
    def recorder(self) -> CaptureRecorder:
        if self._recorder is None:
            raise ServiceStartupError("capture is bound by reset(scenario_id=...)")
        return self._recorder

    # -- control plane ----------------------------------------------------
    def _control_plane(self, method: str, path: str,
                       body: Mapping[str, Any] | None) -> tuple[int, dict[str, Any]]:
        if method == "GET" and path == CONTROL_PREFIX + "/readiness":
            return 200, self.readiness()
        if method == "POST" and path == CONTROL_PREFIX + "/reset":
            payload = dict(body or {})
            return 200, self.reset(scenario_id=str(payload.get("scenarioId", "")))
        if method == "POST" and path == CONTROL_PREFIX + "/seed":
            payload = dict(body or {})
            scenario_id = str(payload.get("scenarioId", ""))
            declared = payload.get("initialState")
            if declared is None:
                _, scenario = scenario_entry(self.bundle.catalog, scenario_id)
                declared = scenario["materialization"]["initialState"]
            return 200, self.seed(scenario_id=scenario_id,
                                  initial_state=[str(item) for item in declared])
        if method == "POST" and path == CONTROL_PREFIX + "/export":
            payload = dict(body or {})
            return 200, self.export(Path(str(payload["outDir"])))
        if method == "POST" and path == CONTROL_PREFIX + "/stop":
            threading.Thread(target=self.stop, name="ubm-stop",
                             daemon=True).start()
            return 200, {"stopping": True}
        return 404, {"code": "AIC_RESOURCE_NOT_FOUND"}

    # -- capture envelope and observations --------------------------------
    def _envelope(self, plan: ScenarioPlan) -> dict[str, Any]:
        provenance = self._release["provenance"]
        return {
            "schemaPath": str(self._schema_path),
            "declaredRuleIds": list(plan.rules),
            "requiresRealCoordinatorExecution": bool(
                plan.scenario_id == "SC-083"),
            "evaluationNow": plan.evaluation_now,
            "authorityAllowlist": list(self._authorities),
            "revisions": {
                "upperReleaseId": self._release["releaseId"],
                "upperReleaseVersion": self._release["releaseVersion"],
                "upperReleaseContentSha256": self._release["releaseContentSha256"],
                "upperReleaseManifestSha256": self._release["manifestSha256"],
                "upperSourceCommit": provenance["sourceCommit"],
                "upperSourceTree": provenance["sourceTree"],
                "testedCodeCommit": provenance["testedCodeCommit"],
                "lowerReleaseName": "lower-local-69-1.0.1-51d73ca",
                "lowerReleaseCommit":
                    "51d73ca098743b25fe074d184904e695af37fd95",
                "lowerReleaseTree": "54ca268c08f03f2610c65c0ef5f0b25c3091d8ef",
                "lowerReleaseArchiveSha256":
                    "3e3bdfbfd8552b356f31cd2f01c249d45ade3dc91d90642b1a8a3d021c2a8b2d",
            },
            "contract": {
                "contractProfile": "oran-aic/1.0.1",
                "correctedHandoff": "1.0.1",
                "handoffManifestSha256": _digest(
                    Path(self.bundle.authority_path) / "handoff-manifest.1.0.1.json"),
                "bundleManifestSha256": _digest(
                    self.bundle.path / "bundle-manifest.1.0.1.json"),
                "catalogSha256": _digest(
                    self.bundle.path / "scenario-catalog.1.0.1.json"),
                "runnerContractSha256": _digest(
                    self.bundle.path / "scenario-runner-contract.1.0.1.json"),
                "profileAssignmentSha256": _digest(
                    self.bundle.path / "execution-profile-assignment.1.0.1.json"),
                "deploymentVectorSchemaSha256": self.vector_schema_sha256,
            },
            "deployment": {
                "vectorVersion": self.vector["vectorVersion"],
                "vectorSha256": self.vector_sha256,
                "basedOnLowerVectorSha256":
                    "288f2fbc120bf49064a0fc4439323b0d4752f583976b9f19972e8e80072f550f",
                "bindingDocSha256": self._binding_document_digest(),
                "placeholderFree": True,
                "resolvedRoots": {
                    "r1ApiRoot": self.vector["r1"]["apiRoot"],
                    "rAppCallbackRoot": self.vector["r1"]["callbackApi"]["rootUri"],
                    "a1ApiRoot": self.vector["a1"]["apiRoot"],
                    "a1StatusCallbackRoot": self.vector["a1"]["statusCallbackRoot"],
                    "policyEvidencePushBaseUri":
                        self.vector["r1"]["dme"]["policyEvidencePushBaseUri"],
                    "mnsRoot": self.vector["o1"]["fileDataReporting"]["mnsRoot"],
                    "o1ConsumerRoot":
                        self.vector["o1"]["fileDataReporting"]["consumerReference"],
                },
            },
            "scenario": {
                "scenarioId": plan.scenario_id,
                "catalogPointer": plan.catalog_pointer,
                "expectedPointer": plan.expected_pointer,
                "rulesPointer": plan.rules_pointer,
                "fixtureMode": "EXACT_BUNDLE",
                "executionProfile": "bilateral-mock",
                "counterpartProvisioning": "UPPER_SUPPLIED_AGREED_MOCK",
                "declaredFaultCount": 0,
            },
        }

    def _binding_document_digest(self) -> str:
        for root in (Path(self.config.spec_dir),
                     Path(__file__).resolve().parents[3] / "docs" / "upper-bilateral-mock"):
            candidate = root / "deployment-binding.1.0.0.json"
            if candidate.is_file():
                return _digest(candidate)
        raise ServiceStartupError("the deployment binding document was not found")

    def _state_snapshot(self) -> dict[str, Any]:
        store = self.nonrt.store
        policy_ids = sorted(str(row["policy_id"]) for row in
                            store.rows("SELECT policy_id FROM policies"))
        accepted: dict[str, int] = {}
        for row in store.rows(
                "SELECT key,value_json FROM metadata WHERE key LIKE 'dme-push:%'"):
            binding = str(row["key"]).split(":", 1)[1]
            accepted[binding] = len(store.decode(row["value_json"]))
        rapp_state = self.r1_client.state.snapshot()
        snapshot = {
            "policyCount": len(policy_ids),
            "statusCount": len(store.rows("SELECT policy_id FROM statuses")),
            "dmeDataJobCount": len(store.rows("SELECT data_job_id FROM data_jobs")),
            "deliveryBindingCount": len(rapp_state.get("bindings", {})),
            "acceptedPushPayloadCounts": accepted,
            "subscriptionCount": len(
                store.rows("SELECT subscription_id FROM subscriptions")),
            "evidenceCommitCount": len(rapp_state.get("evidence", {})),
            "policyIds": policy_ids,
        }
        snapshot["jcsSha256"] = jcs_sha256(snapshot)
        return snapshot

    def _record_simulated_writes(self, recorder: CaptureRecorder) -> None:
        """The upper performs no RAN write, simulated or otherwise.

        E2 control is lower-owned in ``bilateral-mock``; the oracle's
        ``normalRanWrites``/``rollbackRanWrites`` are the lower runner's counts.
        This member records what *this* artifact did, which is nothing.
        """
        recorder.set_simulated_writes([], normal=0, rollback=0)

    def _record_applied_rules(self, recorder: CaptureRecorder) -> None:
        plan = self._plan
        if plan is None:
            return
        state = self._state_snapshot()
        for rule_id in plan.rules:
            recorder.add_applied_rule(
                rule_id,
                inputs=[plan.catalog_pointer + "/rules"],
                observations={
                    "policyCount": state["policyCount"],
                    "dmeDataJobCount": state["dmeDataJobCount"],
                    "evidenceCommitCount": state["evidenceCommitCount"],
                })


class _RecorderProxy:
    """Late-bound recorder view so an ICS built at start can audit per scenario."""

    def __init__(self, service: "UpperBilateralMockService") -> None:
        self._service = service

    @property
    def clock(self) -> LogicalClock:
        recorder = self._service._recorder
        return recorder.clock if recorder is not None else self._service._clock

    def next_sequence(self) -> int:
        recorder = self._service._recorder
        return recorder.next_sequence() if recorder is not None else 0

    def emit_harness_operation(self, record: Mapping[str, Any]) -> int:
        recorder = self._service._recorder
        return recorder.emit_harness_operation(record) if recorder is not None else 0


def _trace_matches(policy: Any, trace: Mapping[str, Any]) -> bool:
    """True when a policy object carries the status snapshot's trace identity."""
    if not isinstance(policy, Mapping):
        return False
    observed = policy.get("trace")
    if not isinstance(observed, Mapping):
        return False
    keys = [key for key in ("intentId", "correlationId") if key in trace]
    return bool(keys) and all(observed.get(key) == trace[key] for key in keys)


def _host_of(authority: str) -> str:
    return authority.rsplit(":", 1)[0]


def _digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _resolve_schema_path(config: UbmStartupConfig) -> Path:
    candidate = Path(config.spec_dir) / "capture-schema.1.0.0.json"
    return candidate if candidate.is_file() else default_schema_path()


def _read_release_manifest(path: Path) -> dict[str, Any]:
    raw = Path(path).read_bytes()
    document = json.loads(raw.decode("utf-8"))
    provenance = document.get("provenance", {})
    missing = [name for name in ("releaseId", "releaseVersion",
                                 "releaseContentSha256")
               if not document.get(name)]
    missing += ["provenance." + name for name in
                ("sourceCommit", "sourceTree", "testedCodeCommit")
                if not provenance.get(name)]
    if missing:
        raise ServiceStartupError(
            "release manifest is missing %s" % ", ".join(missing))
    return {
        "releaseId": str(document["releaseId"]),
        "releaseVersion": str(document["releaseVersion"]),
        # Container-independent digest of the packaged members.  Deliberately
        # NOT the .tar.gz raw-byte digest, and named so that nobody compares
        # it against `sha256sum` output; the container digest is published as
        # `tarballSha256` in OUT-OF-BAND-SHA256.txt.
        "releaseContentSha256": str(document["releaseContentSha256"]),
        "manifestSha256": hashlib.sha256(raw).hexdigest(),
        "provenance": {
            "sourceCommit": str(provenance["sourceCommit"]),
            "sourceTree": str(provenance["sourceTree"]),
            "testedCodeCommit": str(provenance["testedCodeCommit"]),
        },
    }


def _node_key(node_id: Mapping[str, Any]) -> int:
    return int(str(node_id["hex"]), 16)


def _derive_connection(declared: Mapping[str, Any], node: Mapping[str, Any],
                       capability: Mapping[str, Any]) -> dict[str, Any]:
    """Reconcile one declared E2 connection with the pinned static capability.

    The deployment vector supplies the operational members the E2 inventory
    schema requires (epoch, association, setup PDU, module sets); the bundle
    fixture supplies the node identity and RAN-function digests, because the
    contract forces that fixture as the capability source (V-2).
    """
    connection = json.loads(json.dumps(dict(declared)))
    connection["globalE2NodeId"] = json.loads(json.dumps(dict(node["globalE2NodeId"])))
    connection["e2apVersion"] = capability["e2Deployment"]["e2apVersion"]
    connection["transferSyntax"] = capability["serviceModels"]["e2ap"]["encoding"]
    required = {int(item["ranFunctionId"]): item
                for item in node["requiredRanFunctions"]}
    for function in connection.get("ranFunctions", []):
        expected = required.get(int(function.get("ranFunctionId", -1)))
        if expected is None:
            continue
        function["ranFunctionOid"] = expected["ranFunctionOid"]
        function["ranFunctionRevision"] = expected["observedRanFunctionRevision"]
        function.setdefault("rawDefinition", {})["sha256"] = \
            expected["rawDefinitionSha256"]
        function.setdefault("canonicalDefinition", {})["sha256"] = \
            expected["canonicalDecodedDefinitionSha256"]
        function["active"] = True
    connection["active"] = True
    return connection


def _declared_secret_references(vector: Mapping[str, Any],
                                values: Mapping[str, Any]) -> list[str]:
    references = [str(item) for item in vector.get("security", {}).values()]
    for key, value in values.items():
        if key.endswith("Ref") and isinstance(value, str):
            references.append(value)
    return sorted(set(references))


_SOURCE_MAP_CACHE: dict[str, list[dict[str, Any]]] | None = None


def _operation_source_map() -> dict[str, list[dict[str, Any]]]:
    global _SOURCE_MAP_CACHE
    if _SOURCE_MAP_CACHE is None:
        from .ics import spec_path

        document = json.loads(Path(spec_path()).read_text(encoding="utf-8"))
        _SOURCE_MAP_CACHE = {
            scenario: list(entry["initialState"])
            for scenario, entry in document["operationSourceMap"].items()}
    return _SOURCE_MAP_CACHE
