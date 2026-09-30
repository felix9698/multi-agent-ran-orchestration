"""Composition root for the upper-live-o1-harness runtime.

Lifecycle, in this order and no other:

``start -> gate -> readiness -> reset/seed -> execute/capture -> export -> cleanup -> stop``

The identity/authority gate runs **before** any listener binds and before any
outbound byte leaves the process.  It is run under an egress guard whose
allowlist is empty, so ``externalCalls.connectionAttempts`` at the instant the
gate returns is a *measurement* rather than an assumption (G-GATE-1), and a
connect attempted during the gate would abort the gate instead of being tallied
afterwards.

Cleanup runs on both the normal and the failure path and never deletes evidence
(G-CLEAN-1).  ``complete`` is measured as ``residual`` being empty.

The O1 consumer surface is W1's (`oran.release.lo1.o1_consumer`).  It is
imported lazily and may be injected, so this composition root neither duplicates
nor pre-empts that track; when the module is absent the O1 listener is not bound
and readiness says so rather than pretending the surface exists.

The consumer is bound at ``start``, which is *before* ``reset`` mints the run's
recorder, so it is handed the late-bound :class:`_RecorderProxy` rather than a
``None`` that would explode on its first write.  The proxy is the only recorder
seam the O1 track sees, and it forwards the whole frozen ``emit_*``/``set_*``
surface plus the raw store and the running egress guard.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from jsonschema.exceptions import ValidationError

from decision.llm_backend import DeterministicMockBackend, LLMBackendManager
from oran.conformance.contracts import ContractBundle
from oran.rapp.assurance import CombinedAssurance
from oran.rapp.coordinator_adapter import RAppCoordinatorAdapter
from oran.rapp.evidence import EvidenceLedger
from oran.rapp.r1_client import R1Client, R1Error

from . import capture as capture_module
from .capture import (
    CaptureRecorder, ObservedClock, RawStore, default_schema_path,
    upper_body_coverage_is_complete,
)
from .clients import CapturingCallbackSender, CapturingHttpsClient, OutboundRecorderBinding
from .config import Lo1StartupConfig
from .core_a1 import A1StatusInbound, build_a1_client
from .core_coordinator import RealCoordinatorBoundary
from .core_dme import DmeBoundary
from .core_r1 import R1Boundary, evidence_registration, state_snapshot
from .core_stubs import DeterministicStubBoundary
from .egress import EgressGuard, default_gates_path, load_hardware_control_surfaces
from .gate import GATE_SPEC_FILE_NAME, GateRefused, GateResult, run_identity_authority_gate
from .http import (
    SUPPRESS_CAPTURE_HEADER, CaptureContext, CapturePendingBarrier,
    DispatchServer, assert_port_free, bind_tls, serve_in_thread,
)
from .ics import IntegrationControlSurface, load_role_table, readiness_operations
from .o1_readiness import READINESS_ORDER, ReadinessRefused
from .plan import (
    LIVE_O1_SCENARIO, PlanMatcher, ScenarioPlan, TemplateIndex, assert_root_mappings,
    assert_plan_matches_oracle_shape, build_plan, oracle_reference, roots_from_vector,
)
from .preflight import authority_of, base_path_of, load_vector
from .release_config import build_release_config
from .security import SecurityAuthorityError, allowed_tls_roles, load_security_authority
from .tls import SecretResolver, build_client_ssl_context, build_server_ssl_context

STATUS_CALLBACK_SUFFIX = "/r1/a1-policy-status"


class ServiceStartupError(RuntimeError):
    exit_code = 78


@dataclass(frozen=True)
class Lo1Endpoints:
    r1: str
    rapp: str
    o1_consumer: str
    a1_status: str


class UpperLiveO1HarnessService:
    """Own and supervise the upper's listeners for exactly one SC-084 run."""

    def __init__(self, *, config: Lo1StartupConfig, gate: GateResult,
                 bundle: ContractBundle, vector: Mapping[str, Any],
                 vector_sha256: str, resolver: SecretResolver,
                 consumer_factory: Callable[..., Any] | None = None) -> None:
        self.config = config
        self._gate = gate
        self.bundle = bundle
        self.vector = dict(vector)
        self.vector_sha256 = vector_sha256
        self.resolver = resolver
        try:
            self.security_authority = load_security_authority(
                config.security_authority_path,
                expected_sha256=config.security_authority_sha256,
                schema_path=config.spec_dir /
                "o1-security-authority.1.0.0.schema.json")
        except SecurityAuthorityError as exc:
            raise ServiceStartupError(str(exc)) from exc
        self._consumer_factory = consumer_factory
        self._lock = threading.RLock()
        self._admission_local = threading.local()
        self._started = False
        self._stopped = False
        self._servers: list[DispatchServer] = []
        self._threads: list[threading.Thread] = []
        self._exports: dict[str, str] = {}
        self._export_target: Path | None = None
        self._scenario_id: str | None = None
        self.roots = roots_from_vector(self.vector)
        assert_root_mappings(bundle.runner, list(self.roots))
        self.templates = TemplateIndex(bundle.catalog["endpointTemplates"], self.roots)
        self.oracle = oracle_reference(bundle.catalog, LIVE_O1_SCENARIO)
        self.plan: ScenarioPlan = build_plan(
            bundle.catalog, LIVE_O1_SCENARIO, self.roots)
        assert_plan_matches_oracle_shape(self.plan, self.oracle)
        self.clock = ObservedClock()
        self.raw_store = RawStore(capture_root=Path(config.capture_root))
        self._build()

    # -- construction -----------------------------------------------------
    @classmethod
    def from_startup(cls, config: Lo1StartupConfig) -> "UpperLiveO1HarnessService":
        config.validate()
        gate, _observed = run_gate_under_guard(config)
        bundle = ContractBundle(Path(config.contract_authority))
        vector, digest, _raw = load_vector(Path(config.vector_path))
        if digest != config.vector_sha256:
            raise ServiceStartupError("deployment vector digest drifted after load")
        resolver = SecretResolver.from_file(Path(config.secret_map_path))
        return cls(config=config, gate=gate, bundle=bundle, vector=vector,
                   vector_sha256=digest, resolver=resolver)

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
                "the rApp callback, DME push and A1 status roots are the three "
                "co-hosted upper origins and must share one authority")
        consumer_host, consumer_port = authority_of(
            vector["o1"]["fileDataReporting"]["consumerReference"],
            "/o1/fileDataReporting/consumerReference")
        self._endpoints = Lo1Endpoints(
            r1="https://%s:%d" % (r1_host, r1_port),
            rapp="https://%s:%d" % (rapp_host, rapp_port),
            o1_consumer="https://%s:%d" % (consumer_host, consumer_port),
            a1_status="https://%s:%d" % (status_host, status_port))
        self._listen = {
            "r1": (r1_host, r1_port),
            "rapp": (rapp_host, rapp_port),
            "o1_consumer": (consumer_host, consumer_port),
        }
        state_dir = Path(self.config.state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        timeout_ms = int(vector["timeouts"]["defaultStepMs"])
        self._timeout_ms = timeout_ms

        hardware, hardware_source = load_hardware_control_surfaces(default_gates_path())
        self.egress = EgressGuard(allowlist=self._gate.allowlist, hardware=hardware,
                                  hardware_source=hardware_source)
        self.egress.bind_clock(self.clock)
        for authority in self._gate.allowlist:
            role, protocol = self._target_role(authority)
            self.egress.declare_target(authority, protocol=protocol, role=role)

        truststore_ref = str(vector["security"]["truststoreRef"])
        client_context = (build_client_ssl_context(
            truststore_ref=truststore_ref, resolver=self.resolver)
            if self.resolver.has(truststore_ref) else None)
        https_client = CapturingHttpsClient(context=client_context,
                                            timeout_ms=timeout_ms)
        self._outbound = OutboundRecorderBinding()
        self._outbound.templates = self.templates
        self._capture_pending = CapturePendingBarrier()

        a1_client = build_a1_client(api_root=str(vector["a1"]["apiRoot"]),
                                    client=https_client, binding=self._outbound)
        self.callback_sender = CapturingCallbackSender(
            client=https_client, binding=self._outbound,
            observer=self._note_status_callback)
        self.r1 = R1Boundary.compose(
            vector=vector, bundle=self.bundle, state_dir=state_dir,
            a1_client=a1_client, callback_sender=self.callback_sender,
            listen_host=r1_host, base_path=base_path_of(vector["r1"]["apiRoot"]),
            admission=self._admit_measured_body)
        self.a1_status = A1StatusInbound(
            service=self.r1.service, admission=self._admit_measured_body)

        self.r1_client = R1Client(
            api_root=str(vector["r1"]["apiRoot"]),
            r_app_id=str(vector["r1"]["rAppId"]),
            policy_evidence_push_base_uri=str(
                vector["r1"]["dme"]["policyEvidencePushBaseUri"]),
            state_path=state_dir / "rapp-r1.json",
            timeout_s=timeout_ms / 1000.0,
            max_attempts=1, retry_backoff_s=(),
            ssl_context=client_context,
            oauth_header_provider=self._oauth_header_provider(),
            authoritative_status_provider=(
                self._authoritative_status_from_nonrt))
        self.dme = DmeBoundary(
            client=self.r1_client,
            push_base_path=base_path_of(
                vector["r1"]["dme"]["policyEvidencePushBaseUri"]),
            admission=self._admit_measured_body)
        capability_manifest = self.bundle.fixture("fixture://capabilityManifest")
        self.coordinator_adapter = RAppCoordinatorAdapter(
            dispatch=self.r1_client,
            assurance=CombinedAssurance(
                capability_manifest),
            ledger=EvidenceLedger(state_dir / "rapp-evidence.json"),
            capability_manifest=capability_manifest,
            # Seeded, offline and deterministic: the live-O1 profile must never
            # reach a remote model, and two runs must agree.
            llm_manager=LLMBackendManager.with_backend(DeterministicMockBackend(seed=7)),
            config=build_release_config(
                capability_manifest=capability_manifest, vector=vector))
        self.coordinator = RealCoordinatorBoundary(
            adapter=self.coordinator_adapter, recorder=_RecorderProxy(self),
            requires_real_execution=True, admission=self._admit_measured_body)
        self.stubs = DeterministicStubBoundary(
            routing=self._gate.deterministic_stub_routing,
            recorder=_RecorderProxy(self), clock=self.clock,
            admission=self._admit_measured_body)

        self.contexts = {
            "nonrt": CaptureContext(
                component="nonrt", base_uri=self._endpoints.r1,
                authority="%s:%d" % (r1_host, r1_port), timeout_ms=timeout_ms,
                templates=self.templates, egress=self.egress,
                suppress_upper_self=True,
                completion_barrier=lambda child: self._capture_pending.lease(
                    admitted_child=child)),
            "rapp": CaptureContext(
                component="rapp", base_uri=self._endpoints.rapp,
                authority="%s:%d" % (rapp_host, rapp_port), timeout_ms=timeout_ms,
                templates=self.templates, egress=self.egress,
                # The R1 status relay has both ends upper; it is captured once,
                # on the client side, with peer UPPER_SELF.
                suppressed_paths=(base_path_of(
                    vector["r1"]["callbackApi"]["rootUri"]) + STATUS_CALLBACK_SUFFIX,),
                completion_barrier=lambda child: self._capture_pending.lease(
                    admitted_child=child)),
            "o1_consumer": CaptureContext(
                component="o1_consumer", base_uri=self._endpoints.o1_consumer,
                authority="%s:%d" % (consumer_host, consumer_port),
                timeout_ms=timeout_ms, templates=self.templates, egress=self.egress,
                completion_barrier=lambda child: self._capture_pending.lease(
                    admitted_child=child),
                trusted_tls_roles=allowed_tls_roles(self.security_authority)),
        }
        role_table = load_role_table()
        self.control_surfaces = {
            name: IntegrationControlSurface(
                component=name, delegate=self._delegate_for(name),
                recorder=_RecorderProxy(self),
                assigned_adapter_actions=self._gate.netconf_adapter_actions,
                role_table=role_table)
            for name in ("nonrt", "rapp", "o1_consumer")
        }
        self._recorder: CaptureRecorder | None = None
        self.o1_consumer: Any = None
        self._cleanup_targets: list[tuple[str, Callable[[], None]]] = []
        self._cleanup_failure = False
        self._runtime_timed_out = False

    # -- helpers ----------------------------------------------------------
    def _target_role(self, authority: str) -> tuple[str, str]:
        roots = self._gate.observations.get("resolvedRoots", {}) if \
            self._gate.observations else {}
        if authority in tuple(self._gate.observations.get("upperListeners", ())):
            return "UPPER_SELF", "HTTPS"
        netconf = str(roots.get("netconfEndpoint", ""))
        if netconf and authority in netconf:
            return "LOWER_LIVE_O1_PROVIDER", "NETCONF_SSH"
        if authority in set(roots.get("sftpAuthorities", ())):
            return "LOWER_LIVE_O1_PROVIDER", "SFTP_SSH"
        if authority in str(roots.get("mnsRoot", "")):
            return "LOWER_LIVE_O1_PROVIDER", "HTTPS"
        if authority in str(roots.get("a1ApiRoot", "")):
            return "LOWER_IMPLEMENTATION_UNDER_TEST", "HTTPS"
        return "UNATTRIBUTED", "HTTPS"

    def _oauth_header_provider(self) -> Callable[[str, str, str], str]:
        reference = str(self.vector["security"]["r1ClientCredentialRef"])

        def _provider(_method: str, _uri: str, _scope: str) -> str:
            recorder = self._recorder
            if recorder is not None:
                recorder.note_secret_reference(reference)
            token = self.resolver.resolve_path(reference).read_text(
                encoding="utf-8").strip()
            return "Bearer " + token

        return _provider

    def _note_status_callback(self, status: int) -> None:
        with self._lock:
            self._status_callbacks_relayed = getattr(
                self, "_status_callbacks_relayed", 0) + 1
            self._last_status_callback_status = int(status)

    def _authoritative_status_from_nonrt(
            self, policy_id: str) -> Mapping[str, Any]:
        store = self.r1.service.store
        row = store.row(
            "SELECT status_json FROM statuses WHERE policy_id=?", (policy_id,))
        if row is None:
            raise R1Error(
                "authoritative Non-RT status is absent for policy " + policy_id)
        status = store.decode(row["status_json"])
        if not isinstance(status, Mapping):
            raise R1Error(
                "authoritative Non-RT status is not an object for policy "
                + policy_id)
        return status

    def _delegate_for(self, component: str
                      ) -> Callable[[str, Mapping[str, Any]], Mapping[str, Any]]:
        def _dispatch(op: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            return self._component_delegate(component, op, arguments)

        return _dispatch

    def _component_delegate(self, component: str, op: str,
                            arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        if op == "__STATE__":
            return self._component_state(component)
        if op == "RESET_SCENARIO_STATE":
            self._reset_components()
            return {"resetCompleted": True}
        if op == "COORDINATOR_PROCESS_INTENT":
            with self._admit_measured_body("processing an intent in the Coordinator"):
                return self.coordinator.process_intent(arguments)
        if op == "R1_DME_BINDING_PRECREATE":
            with self._admit_measured_body(
                    "precreating an R1 DME delivery binding"):
                self.dme.precreate_binding(
                    str(arguments["deliveryBindingId"]), arguments["job"])
            return {}
        if op == "R1_DME_BINDING_COMMIT":
            with self._admit_measured_body(
                    "committing an R1 DME delivery binding"):
                self.dme.commit_binding(
                    str(arguments["deliveryBindingId"]),
                    str(arguments["dataJobId"]))
            return {}
        if op in {"KPM_SNAPSHOT", "E2_CONTROL_RESULT", "READBACK"}:
            if component != "rapp" or self.stubs.routing != \
                    "UPPER_DETERMINISTIC_STUB":
                raise ServiceStartupError(
                    "%s is not assigned to this upper runtime" % op)
            step_id = str(arguments["stepId"])
            if op == "KPM_SNAPSHOT":
                return self.stubs.kpm_snapshot(
                    step_id=step_id, snapshot=dict(arguments["snapshot"]))
            if op == "E2_CONTROL_RESULT":
                return self.stubs.control(
                    step_id=step_id, target=dict(arguments["target"]),
                    rollback=bool(arguments.get("rollback", False)))
            return self.stubs.readback(
                step_id=step_id, observed=dict(arguments["observed"]))
        if op == "O1_RETRIEVE":
            if component != "o1_consumer" or self.o1_consumer is None:
                raise ServiceStartupError("O1_RETRIEVE has no live consumer")
            return self.o1_consumer.retrieve_latest()
        if op == "O1_NORMALIZE":
            if component != "o1_consumer" or self.o1_consumer is None:
                raise ServiceStartupError("O1_NORMALIZE has no live consumer")
            records = self.o1_consumer.normalize_latest(
                context=self._live_normalization_context())
            return {
                "records": records,
                "commitEligibleRecords": len(records),
                "rejectedRecords": 0,
                "duplicateRecords": 0,
                "samples": [sample for record in records
                            for sample in record.get("samples", ())],
            }
        if component == "nonrt":
            return self._nonrt_seed(op, arguments)
        if component == "o1_consumer" and self.o1_consumer is not None:
            return dict(self.o1_consumer.harness_op({"op": op, **dict(arguments)}
                                                    ).get("outputs", {}))
        raise ServiceStartupError("operation %s is not served by %s" % (op, component))

    def _live_normalization_context(self) -> dict[str, Any]:
        """Build correlation only from the durable R1 policy/status state.

        The Lower runner may trigger normalization, but it cannot supply or
        rewrite the policy identity used by the live O1 evidence.
        """
        store = self.r1.service.store
        policies = store.rows("SELECT * FROM policies ORDER BY policy_id")
        if len(policies) != 1:
            raise ServiceStartupError(
                "live O1 normalization requires exactly one durable policy")
        policy_row = policies[0]
        status_row = store.row(
            "SELECT status_json FROM statuses WHERE policy_id=?",
            (policy_row["policy_id"],))
        if status_row is None:
            raise ServiceStartupError(
                "live O1 normalization requires a durable policy status")
        policy = store.decode(policy_row["policy_json"])
        status = store.decode(status_row["status_json"])
        aic = status.get("aicStatus", {})
        control = aic.get("control", {}) if isinstance(aic, Mapping) else {}
        correlation: dict[str, Any] = {
            "policyTypeId": str(policy_row["policy_type_id"]),
            "policyId": str(policy_row["policy_id"]),
            "policyRevision": int(
                aic.get("policyRevision") or policy.get("trace", {}).get(
                    "policyRevision")),
        }
        for key, value in (
                ("episodeId", aic.get("episodeId")),
                ("transactionId", control.get("transactionId")),
                ("actionId", control.get("actionId"))):
            if value is not None:
                correlation[key] = value
        ue_id = policy.get("scope", {}).get("ueId")
        if not isinstance(ue_id, Mapping):
            raise ServiceStartupError("the durable policy has no UE scope")
        return {"correlation": correlation,
                "policyScope": {"ueId": dict(ue_id)}}

    def _nonrt_seed(self, op: str, arguments: Mapping[str, Any]
                    ) -> Mapping[str, Any]:
        """The upper-owned seed rows, executed against the real framework.

        Each row drives or observes the framework through its OWN public R1
        surface.  The framework's development harness plane is deliberately not
        used: it is loopback-only and two-key approved, and this profile's R1
        listener is wherever the deployment vector puts it.  None of these rows
        is a no-op that reports a success it did not achieve.
        """
        del arguments
        base = self.r1.base_path
        if op == "START_R1_A1_SERVICE":
            # Postcondition: the pinned v1 resources are reachable.
            response = self.r1.service.handle(
                "GET", base + "/a1-policy-management/v1/policy-types",
                headers={"Version": "1.0.0"})
            if response.status != 200:
                raise ServiceStartupError(
                    "the pinned R1/A1 policy-management resources are not reachable")
            return {"ready": True}
        if op == "A1_INSTALL_POLICY_TYPE":
            # Postcondition: GET policy type returns the pinned schemas.  The
            # type is pinned in the frozen bundle, so this OBSERVES the
            # postcondition instead of installing a second copy of it.
            response = self.r1.service.handle(
                "GET", base
                + "/a1-policy-management/v1/policy-types/AIC_UECellSteering_1.0.0",
                headers={"Version": "1.0.0"})
            if response.status != 200:
                raise ServiceStartupError(
                    "the pinned A1 policy type is not served by this framework")
            return {"policyTypeReady": True}
        if op == "LOAD_CAPABILITY":
            # The capability manifest is the frozen bundle fixture, bound at
            # compose time and validated there for bijection and neighbours;
            # this asserts that binding held rather than re-deriving it.
            if not self.r1.service.a1_ready:
                raise ServiceStartupError(
                    "the pinned capability did not satisfy the A1 readiness checks")
            return {"capabilityReady": True}
        if op == "SET_DEPENDENCIES":
            return {"dependenciesReady": bool(self.r1.service.a1_ready)}
        if op == "INSTALL_R1_STATUS_SUBSCRIPTION":
            self._install_status_subscription()
            return {"subscriptionInstalled": True}
        if op == "INSTALL_DME_REGISTRATION":
            # Rebuilt from this release's own frozen bundle and vector, then
            # registered through the public R1 data-registration route.
            response = self.r1.service.handle(
                "POST", base + "/data-registration/v2/production-capabilities",
                headers={"Version": "2.0.0-alpha.2"},
                body=evidence_registration(vector=self.vector, bundle=self.bundle))
            if response.status not in (200, 201):
                raise ServiceStartupError(
                    "the evidence DME type could not be registered")
            return {"registered": True}
        raise ServiceStartupError("Non-RT surface does not serve %s" % op)

    def _install_status_subscription(self) -> None:
        """The pinned status subscription must exist as a durable row."""
        subscription_id = str(self.vector["r1"]["statusSubscriptionId"])
        destination = (str(self.vector["r1"]["callbackApi"]["rootUri"]).rstrip("/")
                       + STATUS_CALLBACK_SUFFIX)
        store = self.r1.service.store
        store.execute(
            "INSERT INTO subscriptions(subscription_id,body_json) VALUES(?,?) "
            "ON CONFLICT(subscription_id) DO UPDATE SET body_json=excluded.body_json",
            (subscription_id, store.encode({"notificationDestination": destination})))

    def _component_state(self, component: str) -> dict[str, Any]:
        if component == "nonrt":
            return self.r1.state_counts()
        if component == "rapp":
            transitions = self.coordinator_adapter.transition_snapshot()
            terminal = [item for item in transitions if item["to"] == "S6"]
            return {
                "transitions": transitions,
                "terminalOutcomeCount": len(terminal),
                "syntheticTransitionCount":
                    self.coordinator.synthetic_transition_count,
                "statusCallbacksRelayed": getattr(
                    self, "_status_callbacks_relayed", 0),
                "statusCallbacksReceived": getattr(
                    self, "_status_callbacks_relayed", 0),
                "lastStatusCallbackStatus": getattr(
                    self, "_last_status_callback_status", None),
                "r1DurableStatusCount": len(
                    self.r1_client.state.snapshot().get("status", {})),
                **self.dme.observations(),
                **self.a1_status.observations(),
                **self.coordinator_adapter.harness_observations(),
            }
        if component == "o1_consumer":
            if self.o1_consumer is None:
                return {"bound": False}
            return dict(self.o1_consumer.harness_state())
        return {}

    def _reset_components(self) -> None:
        self.r1.reset_scenario_state()
        self.coordinator_adapter.harness_reset()
        self.dme.reset()
        self.a1_status.reset()

    # -- dispatch ---------------------------------------------------------
    @contextmanager
    def _admit_measured_body(self, what: str):
        """One re-entrant admission seam for every SC-084 measured operation.

        Boundary objects also use this callable when invoked directly.  The
        thread-local depth prevents a service dispatch followed by a guarded
        boundary method from being counted twice while preserving one atomic
        ledger lease for teardown's in-flight drain.
        """
        depth = int(getattr(self._admission_local, "depth", 0))
        if depth:
            self._admission_local.depth = depth + 1
            try:
                yield
            finally:
                self._admission_local.depth -= 1
            return
        binding = self.o1_consumer
        readiness = getattr(getattr(binding, "consumer", None), "readiness", None)
        if readiness is None:
            raise ReadinessRefused(
                "READINESS_INCOMPLETE",
                "%s is part of the measured SC-084 body and no O1 readiness "
                "ledger is bound" % what)
        self._admission_local.depth = 1
        try:
            with readiness.measured_body(what):
                yield
        finally:
            self._admission_local.depth = 0

    @staticmethod
    def _readiness_refusal(exc: ReadinessRefused
                           ) -> tuple[int, dict[str, str], bytes]:
        payload = {"code": "AIC_%s" % exc.reason_code,
                   "reasonCode": exc.reason_code}
        return 409, {
            "Content-Type": "application/json",
            SUPPRESS_CAPTURE_HEADER: "1",
        }, _dump(payload)

    @staticmethod
    def _control_response(status: int, payload: Mapping[str, Any]
                          ) -> tuple[int, dict[str, str], bytes]:
        # Harness operations have their own audited `harnessOperations` slot and
        # are not members of the frozen HTTP result sequence.  Recording the
        # same call as a generic exchange would both duplicate evidence and
        # consume an unrelated catalog step.
        headers = {"Content-Type": "application/json",
                   SUPPRESS_CAPTURE_HEADER: "1"}
        return int(status), headers, _dump(payload)

    def _r1_dispatch(self, method: str, path: str, headers: Mapping[str, str],
                     body: bytes) -> tuple[int, dict[str, str], bytes]:
        surface = self.control_surfaces["nonrt"]
        if path.startswith("/harness/"):
            status, payload = surface.handle(method, path, _json(body))
            return self._control_response(status, payload)
        try:
            with self._admit_measured_body("serving an R1 request"):
                return self.r1.dispatch(method, path, headers, body)
        except ReadinessRefused as exc:
            return self._readiness_refusal(exc)

    def _rapp_dispatch(self, method: str, path: str, headers: Mapping[str, str],
                       body: bytes) -> tuple[int, dict[str, str], bytes]:
        if path.startswith("/harness/"):
            status, payload = self.control_surfaces["rapp"].handle(
                method, path, _json(body))
            return self._control_response(status, payload)
        try:
            callback_path = (base_path_of(
                self.vector["r1"]["callbackApi"]["rootUri"])
                + STATUS_CALLBACK_SUFFIX)
            if method == "POST" and path.rstrip("/") == callback_path:
                with self._admit_measured_body(
                        "accepting an R1 policy status callback"):
                    version = next((str(value) for key, value in headers.items()
                                    if str(key).lower() == "version"), None)
                    notification = _json(body)
                    if version != "1.0.0" or notification is None:
                        return 400, {"Content-Type": "application/json"}, _dump(
                            {"code": "AIC_SCHEMA_INVALID"})
                    try:
                        self.r1_client.handle_status_notification(notification)
                    except (R1Error, ValidationError, TypeError, ValueError,
                            KeyError):
                        return 400, {"Content-Type": "application/json"}, _dump(
                            {"code": "AIC_SCHEMA_INVALID"})
                return 204, {"Version": "1.0.0"}, b""
            binding = self.dme.binding_of(path)
            if binding is not None and method == "POST":
                with self._admit_measured_body(
                        "accepting an R1 DME evidence push"):
                    status, payload = self.dme.accept_push(
                        binding_id=binding, headers=headers,
                        record=_json(body) or {})
                    if status == 204:
                        self.r1.service.record_dme_push(
                            binding, _json(body) or {})
                return status, ({"Content-Type": "application/json"}
                                if payload else {}), \
                    (_dump(payload) if payload else b"")
            status_path = base_path_of(self.vector["a1"]["statusCallbackRoot"])
            if method == "POST" and path.rstrip("/") == status_path:
                with self._admit_measured_body("inbound A1 policy status"):
                    return int(self.a1_status.accept(_json(body) or {})), {}, b""
        except ReadinessRefused as exc:
            return self._readiness_refusal(exc)
        return 404, {"Content-Type": "application/json"}, _dump(
            {"code": "AIC_RESOURCE_NOT_FOUND"})

    def _o1_dispatch(self, method: str, path: str, headers: Mapping[str, str],
                     body: bytes) -> tuple[int, dict[str, str], bytes]:
        if path.startswith("/harness/"):
            status, payload = self.control_surfaces["o1_consumer"].handle(
                method, path, _json(body))
            return self._control_response(status, payload)
        if self.o1_consumer is None:
            return 503, {"Content-Type": "application/json"}, _dump(
                {"code": "AIC_O1_CONSUMER_NOT_BOUND"})
        # Identity is injected by the TLS handler from the actual SSLSocket.
        # Neither the configured MnS server authority nor a caller header is a
        # client identity: notification clients normally use an ephemeral TCP
        # source port.
        try:
            with self._admit_measured_body(
                    "accepting a notifyFileReady notification"):
                normalized = {str(key).lower(): str(value)
                              for key, value in headers.items()}
                status, payload = self.o1_consumer.accept_notification(
                    headers=headers, raw=body,
                    peer=normalized.get(
                        "x-lo1-trusted-tcp-peer-authority",
                        "unattributed-peer:0"))
        except ReadinessRefused as exc:
            return self._readiness_refusal(exc)
        except Exception as exc:
            # Import lazily with the consumer, preserving the optional module
            # seam while mapping authentication and authorization failures to
            # their contract statuses instead of disguising them as HTTP 500.
            from .o1_consumer import NotificationAuthorizationRefused
            if isinstance(exc, NotificationAuthorizationRefused):
                return int(exc.status), {
                    "Content-Type": "application/problem+json"}, _dump({
                        "code": ("AIC_O1_AUTHENTICATION_FAILED"
                                 if int(exc.status) == 401
                                 else "AIC_O1_AUTHORIZATION_FAILED")})
            raise
        return int(status), {
            "Content-Type": "application/json",
            SUPPRESS_CAPTURE_HEADER: "1",
        }, _dump(payload)

    def _admitted_mns_authority(self) -> str:
        mns_root = str(self.vector["o1"]["fileDataReporting"]["mnsRoot"])
        host, port = authority_of(mns_root, "/o1/fileDataReporting/mnsRoot")
        authority = "[%s]:%d" % (host, port) if ":" in host else "%s:%d" % (host, port)
        return authority if authority in set(self._gate.allowlist) \
            else "unattributed-peer:0"

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._started:
                raise ServiceStartupError("the service is already started")
            self._gate.require_admitted()
            for host, port in self._listen.values():
                assert_port_free(host, int(port))
            self.egress.install()
            self.egress.begin_scenario()
            dispatchers = {
                "r1": self._r1_dispatch,
                "rapp": self._rapp_dispatch,
                "o1_consumer": self._o1_dispatch,
            }
            contexts = {"r1": self.contexts["nonrt"], "rapp": self.contexts["rapp"],
                        "o1_consumer": self.contexts["o1_consumer"]}
            try:
                for name, (host, port) in self._listen.items():
                    server = DispatchServer((host, int(port)), dispatchers[name],
                                            contexts[name])
                    try:
                        bind_tls(server, self._server_context(name))
                    except BaseException:
                        # This socket is bound but nothing is serving on it yet,
                        # so it is closed directly.  Leaving it to the garbage
                        # collector would hold the port -- and the operator's
                        # next start would refuse with "port in use", blaming
                        # the deployment for the previous refusal.
                        server.server_close()
                        raise
                    self._servers.append(server)
                    self._threads.append(serve_in_thread(server))
                    self._cleanup_targets.append(
                        ("LISTENER", lambda server=server: server.shutdown()))
                self._bind_o1_consumer()
            except BaseException:
                self._abort_start()
                raise
            self._started = True

    def _abort_start(self) -> None:
        """Undo a composition that refused half-way.

        ``start`` is fail-closed, and fail-closed has to mean the process holds
        nothing afterwards: every listener this call bound is shut down and
        closed, the guard is disarmed, and the cleanup ledger loses the entries
        that would otherwise fire a second time from ``stop``.
        """
        while self._servers:
            server = self._servers.pop()
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001 - pragma: no cover, defensive
                pass
            try:
                server.server_close()
            except OSError:  # pragma: no cover - defensive
                pass
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads.clear()
        self._cleanup_targets = [entry for entry in self._cleanup_targets
                                 if entry[0] != "LISTENER"]
        self.egress.uninstall()

    def _bind_o1_consumer(self) -> None:
        """Attach W1's O1 consumer when it is available; never fake it.

        Three things were previously left dangling here and are now wired: the
        recorder (``None`` at ``start``, hence the late-bound proxy), the frozen
        bundle bytes the consumer parses PM files and the NETCONF lifecycle from,
        and the harness-operation surface the integration control surface
        dispatches into.
        """
        factory = self._consumer_factory
        if factory is None:
            try:
                from .o1_consumer import O1LiveConsumer  # type: ignore
            except ImportError:
                self.o1_consumer = None
                return
            factory = O1LiveConsumer
        consumer = factory(
            gate=self._gate, vector=self._canonical_o1_vector(),
            recorder=_RecorderProxy(self),
            resolver=self.resolver, profile_bytes=self._profile_bytes(),
            security_authority=self.security_authority)
        binding = _O1ConsumerBinding(consumer=consumer)
        binding.start()
        self.o1_consumer = binding
        # Cleanup runs on BOTH paths and must know WHICH path it is on, so the
        # flag is read at teardown time rather than frozen at bind time.
        self._cleanup_targets.append(
            ("O1_SUBSCRIPTION",
             lambda: self.o1_consumer.cleanup(failure=self._cleanup_failure)))

    #: The frozen bundle members the O1 track reads.  Handed over as bytes so
    #: the consumer parses the SAME bytes the gate digested, and so nothing in
    #: the O1 path re-derives a contract value from a second source.
    PROFILE_BUNDLE_MEMBERS = (
        "oran-aic-o1-pa-file.1.0.0.json",
        "o1-netconf-yang-profile.1.0.0.json",
        "golden/o1/valid-prb.xml",
        "golden/o1/null-prb.xml",
        "golden/o1/suspect-prb.xml",
        "golden/o1/notify-file-ready.json",
    )

    def _canonical_o1_vector(self) -> dict[str, Any]:
        """Bind Provider DNs to the frozen capability's cell identities.

        The deployment supplies the Provider-visible full DNs, but it cannot
        redefine which frozen cellId each DN names.  Accept the deployment DN
        set only when it is exactly the two policy-cell DNs in the capability,
        then hand the O1 normalizer the canonical pairs.
        """
        vector = json.loads(json.dumps(self.vector))
        if self._gate.provider_kind != \
                "LIVE_O1_PROVIDER_UNDER_SEPARATE_AUTHORITY":
            return vector
        topology = vector.get("topology", {})
        wanted = [
            topology.get("servingCell", {}).get("cellId"),
            topology.get("targetCell", {}).get("cellId"),
        ]
        wanted_keys = {
            json.dumps(item, sort_keys=True, separators=(",", ":"))
            for item in wanted if isinstance(item, Mapping)
        }
        if len(wanted_keys) != 2:
            raise ServiceStartupError(
                "the O1 policy cells are not two distinct frozen identities")

        source = list(topology.get("cellMappings", ()))
        source_policy = [
            item for item in source
            if json.dumps(item.get("cellId"), sort_keys=True,
                          separators=(",", ":")) in wanted_keys
        ]
        capability = self.bundle.fixture("fixture://capabilityManifest")
        canonical = [
            item for item in capability.get("topology", {}).get("cells", ())
            if json.dumps(item.get("cellId"), sort_keys=True,
                          separators=(",", ":")) in wanted_keys
        ]
        source_dns = {
            item.get("managedObjectDn") for item in source_policy
            if isinstance(item.get("managedObjectDn"), str)
        }
        canonical_dns = {
            item.get("managedObjectDn") for item in canonical
            if isinstance(item.get("managedObjectDn"), str)
        }
        if len(source_policy) != 2 or len(canonical) != 2 \
                or source_dns != canonical_dns or len(canonical_dns) != 2:
            raise ServiceStartupError(
                "Provider policy-cell DNs differ from the frozen capability")

        non_policy = [
            item for item in source
            if json.dumps(item.get("cellId"), sort_keys=True,
                          separators=(",", ":")) not in wanted_keys
        ]
        topology["cellMappings"] = non_policy + [
            {
                "cellId": json.loads(json.dumps(item["cellId"])),
                "managedObjectDn": item["managedObjectDn"],
            }
            for item in canonical
        ]
        return vector

    def _profile_bytes(self) -> dict[str, bytes]:
        root = Path(self.bundle.path)
        payload: dict[str, bytes] = {}
        for relative in self.PROFILE_BUNDLE_MEMBERS:
            candidate = root / relative
            if candidate.is_file():
                payload[relative] = candidate.read_bytes()
        catalog = root / ("scenario-catalog.%s.json" % self.bundle.version)
        if catalog.is_file():
            payload["scenario-catalog.1.0.1.json"] = catalog.read_bytes()
        # The readiness postconditions are READ from the runner contract's own
        # bytes rather than restated, so the O1 track is handed them too.
        runner = root / ("scenario-runner-contract.%s.json" % self.bundle.version)
        if runner.is_file():
            payload["scenario-runner-contract.1.0.1.json"] = runner.read_bytes()
        netconf_dir = root / "golden" / "o1" / "netconf"
        if netconf_dir.is_dir():
            for fixture in sorted(netconf_dir.glob("*.xml")):
                payload["golden/o1/netconf/%s" % fixture.name] = fixture.read_bytes()
        return payload

    #: Server TLS material has no slot in the frozen deployment-vector schema,
    #: which declares only client-side trust and credentials.  It is therefore
    #: supplied through the operator's secret map under these reference names.
    #: They are references, never material, and never packaged.
    SERVER_TLS_REFERENCES = {
        "r1": ("secret://tls/r1-certificate", "secret://tls/r1-private-key"),
        "rapp": ("secret://tls/rapp-certificate", "secret://tls/rapp-private-key"),
        "o1_consumer": ("secret://tls/o1-consumer-certificate",
                        "secret://tls/o1-consumer-private-key"),
    }

    def _server_context(self, name: str) -> Any:
        if name == "o1_consumer":
            profile = self.security_authority["tls"]["notificationReceiver"]
            certificate_ref = str(profile["serverCertificateRef"])
            key_ref = str(profile["serverPrivateKeyRef"])
            truststore_ref = str(profile["clientTruststoreRef"])
            require_client_certificate = True
        else:
            certificate_ref, key_ref = self.SERVER_TLS_REFERENCES[name]
            truststore_ref = str(self.vector["security"]["truststoreRef"])
            require_client_certificate = False
        if not (self.resolver.has(certificate_ref) and self.resolver.has(key_ref)):
            # Fail closed.  Every capture records ``transport.scheme: https``,
            # so a listener that fell back to plaintext would make the evidence
            # untrue; there is no fallback and no plaintext profile.
            raise ServiceStartupError(
                "listener %s has no server TLS material; the secret map must "
                "resolve %s and %s" % (name, certificate_ref, key_ref))
        recorder = self._recorder
        if recorder is not None:
            recorder.note_secret_reference(certificate_ref)
        return build_server_ssl_context(
            certificate_ref=certificate_ref, private_key_ref=key_ref,
            truststore_ref=truststore_ref, resolver=self.resolver,
            require_client_certificate=require_client_certificate)

    def readiness(self) -> dict[str, Any]:
        revisions = self._gate.observations.get("revisions")
        if not isinstance(revisions, Mapping):
            raise ServiceStartupError(
                "the admitted gate retained no upper release identity")
        release_version = revisions.get("upperReleaseVersion")
        if not isinstance(release_version, str) or not release_version:
            raise ServiceStartupError(
                "the admitted gate retained no upper release version")
        return {
            "releaseId": "upper-live-o1-harness",
            "releaseVersion": release_version,
            "profile": self.config.profile,
            "providerKind": self._gate.provider_kind,
            "selfTestLabel": self._gate.self_test_label,
            "gateResult": "ADMITTED" if self._gate.admitted else "REFUSED",
            "scenario": self.plan.scenario_shape(),
            "oracle": dict(self.oracle),
            "endpoints": {
                "r1": self._endpoints.r1, "rapp": self._endpoints.rapp,
                "o1Consumer": self._endpoints.o1_consumer,
                "a1Status": self._endpoints.a1_status,
            },
            "deterministicStubRouting": self._gate.deterministic_stub_routing,
            "netconfAdapterActions": sorted(self._gate.netconf_adapter_actions),
            # §10.2 readiness, as it stands right now.  An operator reading this
            # before driving the measured body is told exactly which
            # postcondition is still owed rather than discovering it at the
            # first refused notification.
            "o1Readiness": {
                "unmet": self._readiness_unmet(),
                "measuredBodyAdmitted": bool(
                    self.o1_consumer is not None
                    and dict(self.o1_consumer.harness_state()).get(
                        "measuredBodyAdmitted")),
            },
            # Server TLS material has no slot in the frozen deployment vector, so
            # the operator has to be told what to supply.  References and their
            # resolution status only -- never a path and never material.
            "listenerTlsReferences": {
                name: {
                    "certificateRef": certificate, "privateKeyRef": key,
                    "resolved": bool(self.resolver.has(certificate)
                                     and self.resolver.has(key)),
                }
                for name, (certificate, key) in sorted(
                    self.SERVER_TLS_REFERENCES.items())
            },
            "o1ConsumerBound": self.o1_consumer is not None,
            "unresolved": self._gate.unresolved_ledger(),
            "externalCalls": self.egress.observations(scope="scenario"),
        }

    def reset(self, *, scenario_id: str) -> dict[str, Any]:
        if scenario_id != LIVE_O1_SCENARIO:
            raise ServiceStartupError(
                "this release runs exactly one scenario: %s" % LIVE_O1_SCENARIO)
        with self._lock:
            self._reset_components()
            self.egress.begin_scenario()
            recorder = CaptureRecorder(
                run_id=self.config.run_id, scenario_id=scenario_id,
                envelope=self._envelope(), clock=self.clock,
                raw_store=self.raw_store)
            recorder.bind_egress_guard(self.egress)
            recorder.set_gate_result(self._gate)
            recorder.set_netconf_assignment(
                self._gate.observations["netconfAssignment"])
            # The proxy drops writes made before this recorder existed, so the
            # readiness ledger the consumer has already begun is re-established
            # here rather than being silently lost between start and reset.
            readiness = getattr(self.o1_consumer, "readiness_capture", None)
            if callable(readiness):
                recorder.set_o1_readiness(readiness())
            for reference in self._declared_secret_references():
                recorder.note_secret_reference(reference)
            recorder.set_state(when="before", snapshot=self._state_snapshot())
            self._recorder = recorder
            self._scenario_id = scenario_id
            matcher = PlanMatcher(self.plan)
            for context in self.contexts.values():
                context.recorder = recorder
                context.matcher = matcher
            self._outbound.bind(recorder=recorder, matcher=matcher,
                                templates=self.templates,
                                completion_barrier=self._capture_pending.lease)
            return {"scenarioId": scenario_id, "reset": True,
                    "state": recorder.snapshot()["state"]["before"]}

    def seed(self, *, scenario_id: str,
             initial_state: Sequence[str]) -> dict[str, Any]:
        """Run the upper-owned adapter actions of the scenario's initial state."""
        role_table = load_role_table()
        by_id = {str(entry["id"]): entry for entry in role_table["initialStates"]}
        applied: list[str] = []
        skipped: list[str] = []
        for state_id in initial_state:
            entry = by_id.get(str(state_id))
            if entry is None:
                raise ServiceStartupError("unknown initial state %s" % state_id)
            obligation = str(entry["upperObligation"])
            action = str(entry["adapterAction"])
            if obligation == "REQUIRED_SEED_OP":
                self._apply_seed(action)
                applied.append(action)
                continue
            if obligation == "REQUIRED_READINESS_ASSIGNED":
                # The gate refuses an empty or partial assignment, so reaching
                # here without the action assigned is a composition fault, not a
                # configuration choice.  It fails closed rather than skipping.
                if action not in self._gate.netconf_adapter_actions:
                    raise ServiceStartupError(
                        "readiness adapter action %s is mandatory for SC-084 but "
                        "the admitted assignment does not carry it" % action)
                self._apply_seed(action)
                applied.append(action)
                continue
            skipped.append(action)
        return {"scenarioId": scenario_id, "applied": applied, "skipped": skipped}

    #: Which control surface serves each adapter action.  Derived from the role
    #: table's own component split: the R1/A1 framework rows are the Non-RT
    #: surface's, and the O1 rows are the consumer's.
    SEED_COMPONENTS = {
        "START_R1_A1_SERVICE": "nonrt",
        "A1_INSTALL_POLICY_TYPE": "nonrt",
        "LOAD_CAPABILITY": "nonrt",
        "SET_DEPENDENCIES": "nonrt",
        "INSTALL_R1_STATUS_SUBSCRIPTION": "nonrt",
        "INSTALL_DME_REGISTRATION": "nonrt",
        "LOAD_O1_PROFILE": "o1_consumer",
        "INSTALL_O1_SUBSCRIPTION": "o1_consumer",
        "SET_PERF_METRIC_JOB": "o1_consumer",
    }

    def _apply_seed(self, action: str) -> None:
        """Drive one adapter action through its audited control surface."""
        component = self.SEED_COMPONENTS.get(action)
        if component is None:
            raise ServiceStartupError(
                "adapter action %s has no upper implementation bound" % action)
        surface = self.control_surfaces[component]
        status, payload = surface.handle(
            "POST", "/harness/op", {"op": action, **self._seed_arguments(action)})
        if status != 200:
            raise ServiceStartupError(
                "seed %s was refused with %d: %s" % (action, status, payload))

    def _seed_arguments(self, action: str) -> dict[str, Any]:
        arguments: dict[str, Any] = {"scenarioId": LIVE_O1_SCENARIO}
        if action == "INSTALL_O1_SUBSCRIPTION":
            file_data = self.vector["o1"]["fileDataReporting"]
            arguments.update({
                "subscriptionId": file_data["subscriptionId"],
                "consumerReference": file_data["consumerReference"]})
        elif action == "SET_PERF_METRIC_JOB":
            arguments.update(dict(self.vector["o1"]["perfMetricJob"]))
        return arguments

    def export(self, out_dir: Path) -> dict[str, str]:
        target = Path(out_dir) / ("capture-%s-%s.json" % (
            self.recorder.run_id, self.recorder.scenario_id))
        try:
            self._close_measured_admission()
        except Exception as exc:  # noqa: BLE001 - the refusal is evidence
            self._record_admission_drain_failure(exc, target=target)
            raise ServiceStartupError(
                "measured admission did not drain before export: %s" % exc) from None
        with self._lock:
            recorder = self._recorder
            if recorder is None:
                raise ServiceStartupError("no scenario has been reset for capture")
            # The measured boundary is frozen before export.  The full-session
            # wire is refreshed again after teardown, so its descriptors include
            # teardown while this boundary remains the pre-teardown prefix.
            self._record_netconf_capture(freeze_measured_end=True)
            recorder.set_state(when="after", snapshot=self._state_snapshot())
            self.stubs.publish()
            self._record_applied_rules(recorder)
            digest = recorder.export(target)
            self._exports[recorder.scenario_id] = digest
            self._export_target = target
            return {"scenarioId": recorder.scenario_id,
                    "capturePath": str(target), "captureSha256": digest}

    def stop(self, *, failure: bool = False) -> None:
        """Cleanup on BOTH paths.  Evidence is never deleted on failure."""
        try:
            self._close_measured_admission()
        except Exception as exc:  # noqa: BLE001 - the refusal is evidence
            self._record_admission_drain_failure(exc, target=self._export_target)
            raise ServiceStartupError(
                "measured admission did not drain before cleanup: %s" % exc) from None
        with self._lock:
            if self._stopped:
                return
            self._cleanup_failure = self._cleanup_failure or bool(failure)
            residual: list[str] = []
            recorder = self._recorder
            capture_failure: Exception | None = None
            try:
                self._record_netconf_capture(freeze_measured_end=True)
            except Exception as exc:  # noqa: BLE001 - failure is durable evidence
                capture_failure = exc
                residual.append("NETCONF_CAPTURE_BOUNDARY: %s" % exc)
            cleanup_blocked = False
            for target, action in reversed(self._cleanup_targets):
                outcome = "DONE"
                try:
                    action()
                except Exception as exc:  # noqa: BLE001 - a failure is evidence
                    outcome = "FAILED"
                    residual.append("%s: %s" % (target, exc))
                    cleanup_blocked = True
                if recorder is not None:
                    recorder.emit_cleanup_action({
                        "target": target, "action": "CLOSE" if target == "LISTENER"
                        else "DELETE", "outcome": outcome,
                        "at": self.clock.now()})
                if cleanup_blocked:
                    # Ordered teardown is conditional.  If the O1 lifecycle
                    # cannot quiesce/delete/read back a resource, do not proceed
                    # into listener close or guard removal and thereby strand an
                    # in-flight operation without its observed boundary.
                    break
            if not cleanup_blocked:
                for server in self._servers:
                    try:
                        server.server_close()
                    except OSError:  # pragma: no cover - defensive
                        residual.append("LISTENER: socket close failed")
            # Residual is MEASURED, including what the O1 track could not remove.
            o1_residual = getattr(self.o1_consumer, "residual", None)
            if callable(o1_residual):
                residual.extend("O1: %s" % item for item in o1_residual())
            if not cleanup_blocked:
                self.egress.uninstall()
            if recorder is not None:
                # The transport retains plaintext octets after close.  Refresh
                # now so the raw descriptors cover the complete session,
                # including every teardown RPC, without moving the boundary.
                try:
                    self._record_netconf_capture(freeze_measured_end=False)
                except Exception as exc:  # noqa: BLE001 - fail closed below
                    capture_failure = capture_failure or exc
                    residual.append("NETCONF_CAPTURE_FINAL: %s" % exc)
                recorder.set_cleanup_path(
                    path="FAILURE" if self._cleanup_failure else "NORMAL",
                    complete=not residual, residual=residual)
                # ``COMPLETED`` is a claim about the whole SC-084 run and SC-084's
                # mandatory O1 initial states are part of it.  A clean shutdown
                # that never established readiness is an aborted run, not a
                # completed one, and the unmet state is named in the residual so
                # the operator is told which postcondition was missing.
                unmet = self._readiness_unmet()
                if unmet:
                    residual.append(
                        "O1_READINESS: %s unconfirmed" % ", ".join(unmet))
                    recorder.set_cleanup_path(
                        path="FAILURE" if self._cleanup_failure else "NORMAL",
                        complete=False, residual=residual)
                observed = recorder.snapshot()
                body_complete, body_failures = upper_body_coverage_is_complete(
                    observed, require_final=True)
                if not body_complete:
                    residual.extend(
                        "UPPER_BODY: %s" % item for item in body_failures)
                external = observed.get("externalCalls") or {}
                guard_failure = bool(
                    int(external.get("hardwareCalls", 0))
                    or int(external.get("forbiddenEgressAttempts", 0))
                    or list(external.get("violations") or [])
                    or list(external.get("peerAttributionAmbiguities") or []))
                if guard_failure:
                    residual.append("EGRESS_GUARD: violation or ambiguity observed")
                within_window = bool(
                    (observed.get("run") or {}).get("executionWindow", {})
                    .get("withinWindow"))
                if not within_window:
                    residual.append(
                        "EXECUTION_WINDOW: observed run ended outside approval")
                if self._runtime_timed_out:
                    residual.append(
                        "RUNTIME_WATCHDOG: maximum duration or approval window "
                        "expired before an authority signal")
                residual = list(dict.fromkeys(residual))
                recorder.set_cleanup_path(
                    path="FAILURE" if (self._cleanup_failure or residual) else "NORMAL",
                    complete=not residual, residual=residual)
                if self._runtime_timed_out or not within_window:
                    disposition = "ABORTED_TIMEOUT"
                elif guard_failure:
                    disposition = "ABORTED_GUARD"
                elif self._cleanup_failure or residual:
                    disposition = "ABORTED_ERROR"
                else:
                    disposition = "COMPLETED"
                recorder.set_disposition(disposition)
                # The lifecycle is export -> cleanup -> stop, so a document
                # exported before cleanup would carry neither the cleanup
                # evidence nor the final disposition.  The same path is
                # rewritten rather than a second file being produced, so the
                # evidence bundle has exactly one capture per scenario.
                target = self._export_target
                if target is not None:
                    self._exports[recorder.scenario_id] = recorder.export(target)
            if cleanup_blocked or capture_failure is not None:
                raise ServiceStartupError(
                    "ordered cleanup stopped with residual: %s"
                    % ", ".join(residual))
            self._stopped = True
            self._started = False

    def _record_admission_drain_failure(
            self, exc: Exception, *, target: Path | None) -> None:
        """Make a drain timeout durable without starting destructive cleanup.

        A lease that has already crossed the measured-body boundary owns the
        right to finish.  If it does not drain within the declared timeout, the
        harness must leave listeners and the egress guard in place so teardown
        can be retried.  The refusal itself is still evidence: record exactly
        one failed DRAIN action, the residual and an aborted disposition.
        """
        with self._lock:
            self._cleanup_failure = True
            recorder = self._recorder
            if recorder is None:
                return
            detail = "IN_FLIGHT_WORK: %s" % exc
            recorder.emit_cleanup_action({
                "target": "IN_FLIGHT_WORK", "action": "DRAIN",
                "outcome": "FAILED", "at": self.clock.now()})
            recorder.set_cleanup_path(
                path="FAILURE", complete=False, residual=[detail])
            recorder.set_disposition("ABORTED_ERROR")
            if target is not None:
                self._persist_drain_incident(Path(target).parent, detail)
            # A previously exported path is rewritten so callers cannot retain
            # a stale pre-timeout disposition.  A first export may not yet have
            # a stable measured end; in that case the in-memory recorder is the
            # durable retry state and the eventual successful retry writes it.
            if target is not None and self._export_target is not None:
                try:
                    self._exports[recorder.scenario_id] = recorder.export(target)
                except Exception:  # noqa: BLE001 - never mask the drain cause
                    pass

    def _persist_drain_incident(self, directory: Path, detail: str) -> Path:
        """Atomically persist a minimal incident while the capture is mutable."""
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        recorder = self.recorder
        target = root / ("incident-%s-%s-drain-failure.json" % (
            recorder.run_id, recorder.scenario_id))
        temporary = target.with_name(target.name + ".tmp")
        payload = (json.dumps({
            "specVersion": "oran-aic-upper-live-o1-drain-incident/1.0.0",
            "runId": recorder.run_id,
            "scenarioId": recorder.scenario_id,
            "observedAt": self.clock.now(),
            "disposition": "ABORTED_ERROR",
            "reasonCode": "IN_FLIGHT_WORK_DRAIN_TIMEOUT",
            "detail": str(detail),
            "httpCaptureOperationsInFlight": self._capture_pending.in_flight,
            "evidencePreservedForRetry": True,
        }, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        try:
            directory_fd = os.open(str(root), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:  # pragma: no cover - unsupported directory fsync
            pass
        return target

    def note_runtime_timeout(self) -> None:
        """Record that the external execution window ended without a signal."""
        with self._lock:
            self._runtime_timed_out = True
            if self._recorder is not None:
                self._recorder.set_disposition("ABORTED_TIMEOUT")

    def _close_measured_admission(self) -> None:
        """Close the body before any end counter is frozen.

        This intentionally runs outside the service lock: an already-admitted
        delegate may need that lock to finish, and teardown must wait for it
        rather than deadlock while holding the supervisor lock.
        """
        timeout_seconds = float(self._timeout_ms) / 1000.0
        # First prevent and drain every HTTP handler through response capture.
        # Only then close the semantic measured-body gate used by non-HTTP
        # delegates.  No late row can cross the frozen sequence boundary.
        self._capture_pending.close_and_wait(timeout_seconds=timeout_seconds)
        readiness = getattr(
            getattr(self.o1_consumer, "consumer", None), "readiness", None)
        if readiness is not None:
            readiness.close_measured_admission(
                timeout_seconds=timeout_seconds)

    def _readiness_unmet(self) -> list[str]:
        """Which §10.2 readiness states this run never confirmed.

        A run with no O1 consumer bound has confirmed none of them, so the whole
        declared order is unmet -- absence is not a pass.
        """
        state = ({} if self.o1_consumer is None
                 else dict(self.o1_consumer.harness_state()))
        unmet = state.get("readinessUnmet")
        if unmet is None:
            return list(READINESS_ORDER)
        return [str(item) for item in unmet]

    # -- capture envelope --------------------------------------------------
    @staticmethod
    def _capture_schema_path() -> Path:
        """Select schema 2 once its recorder API is present in the closure.

        The evidence track supplies the incompatible schema and recorder setter
        as one atomic change.  This feature test keeps this boundary commit
        independently testable, while an integrated 1.0.3 closure necessarily
        resolves ``corrected_schema_path`` and therefore cannot emit schema 1.
        """
        selector = getattr(capture_module, "corrected_schema_path", None)
        if (callable(selector)
                and hasattr(CaptureRecorder, "set_netconf_measured_segment")):
            return Path(selector())
        return Path(default_schema_path())

    def _envelope(self) -> dict[str, Any]:
        observed = dict(self._gate.observations or {})
        contract = dict(observed["contractDigests"])
        contract.update({
            "contractProfile": "oran-aic/1.0.1",
            "correctedHandoff": "1.0.1",
            "frozenBundleDiffFileCount": int(
                observed.get("frozenBundleDiffFileCount", 0)),
        })
        release_root = Path(self.config.release_root)
        provenance_path = release_root / "ARTIFACT-PROVENANCE.json"
        manifest_path = Path(self.config.release_manifest_path)
        try:
            provenance_raw = provenance_path.read_bytes()
            manifest_raw = manifest_path.read_bytes()
        except OSError as exc:
            raise ServiceStartupError(
                "packaged release identity bytes are unavailable: %s" % exc
            ) from exc
        revisions = dict(observed["revisions"])
        revisions.update({
            "identityMode": "PACKAGED_RELEASE",
            "sourceTreeDirty": False,
            "artifactProvenanceRaw": self.raw_store.put(
                provenance_raw, kind="release",
                name="ARTIFACT-PROVENANCE.json",
                media_type="application/json"),
            "releaseManifestRaw": self.raw_store.put(
                manifest_raw, kind="release", name="RELEASE-MANIFEST.json",
                media_type="application/json"),
        })
        return {
            "schemaPath": str(self._capture_schema_path()),
            "revisions": revisions,
            "contract": contract,
            "provider": dict(observed["provider"]),
            "deployment": {
                "vectorVersion": str(self.vector["vectorVersion"]),
                "vectorSha256": self.vector_sha256,
                "bindingDocSha256": self.oracle["expectedJcsSha256"],
                "placeholderFree": True,
                "resolvedRoots": dict(observed["resolvedRoots"]),
                "originOwnership": dict(observed["originOwnership"]),
                "secretRefsResolved": list(observed.get("secretRefsResolved", [])),
                "unresolvedSecretRefs": list(observed.get("unresolvedSecretRefs", [])),
            },
            "scenario": self.plan.scenario_shape(),
            "authorityAllowlist": list(self._gate.allowlist),
            "declaredRuleIds": list(self.plan.rules),
            "requiresRealCoordinatorExecution": True,
        }

    def _record_netconf_capture(self, *, freeze_measured_end: bool) -> None:
        """Translate the transport's observations into capture-schema 2.0.

        O1 owns the live transport and exposes observation-only hooks.  This
        composition root owns serialization: it persists both hello messages
        and both complete plaintext subsystem streams in ``RawStore``, derives
        their shared generation id from the actual hello bytes, and maps the
        measured prefix counters to the schema's exact field names.
        """
        recorder = self._recorder
        consumer = self.o1_consumer
        if recorder is None or consumer is None:
            return
        setter = getattr(recorder, "set_netconf_measured_segment", None)
        if not callable(setter):
            # Schema 1 has no legal slots for the bidirectional session or
            # measured boundary.  The integrated 1.0.3 closure always has this
            # setter; older closures remain readable instead of being relabelled.
            return
        binding = dict(consumer.netconf_session_binding())
        if not binding:
            return
        client_hello = bytes(binding.get("clientHello") or b"")
        server_hello = bytes(binding.get("serverHello") or b"")
        if not client_hello and not server_hello:
            # The adapter is bound at service start, before LOAD_O1_PROFILE
            # opens a transport.  An aborted pre-readiness run has no session
            # to serialize; absence is represented by the schema's null slots.
            setter(None)
            return
        if not client_hello or not server_hello:
            raise ServiceStartupError(
                "a bound NETCONF session has no complete bidirectional hello evidence")
        if freeze_measured_end:
            consumer.mark_measured_segment_end()
        observed_wire = dict(consumer.netconf_wire_evidence())
        if not observed_wire.get("available"):
            raise ServiceStartupError(
                "NETCONF plaintext wire evidence is unavailable: %s"
                % observed_wire.get("reason", "UNSPECIFIED"))
        client_wire = bytes(observed_wire.get("clientToServerOctets") or b"")
        server_wire = bytes(observed_wire.get("serverToClientOctets") or b"")
        if not client_wire or not server_wire:
            raise ServiceStartupError("NETCONF plaintext wire evidence is empty")
        counters = dict(observed_wire.get("counters") or {})
        required_counter_names = (
            "clientMessages", "serverMessages", "clientChunks", "serverChunks",
            "clientHelloEomCount", "serverHelloEomCount",
            "clientPostHelloEomCount", "serverPostHelloEomCount",
        )
        missing = [name for name in required_counter_names if name not in counters]
        if missing:
            raise ServiceStartupError(
                "NETCONF wire evidence lacks measured counters: %s"
                % ", ".join(missing))

        put = recorder.raw_store.put
        client_hello_raw = put(
            client_hello, kind="netconf", name="hello-client.xml",
            media_type="application/xml")
        server_hello_raw = put(
            server_hello, kind="netconf", name="hello-server.xml",
            media_type="application/xml")
        client_wire_raw = put(
            client_wire, kind="netconf", name="session-client-to-server.bin",
            media_type="application/octet-stream")
        server_wire_raw = put(
            server_wire, kind="netconf", name="session-server-to-client.bin",
            media_type="application/octet-stream")
        session_id = hashlib.sha256(client_hello + server_hello).hexdigest()
        advertised = [str(item) for item in
                      binding.get("advertisedCapabilities", ())]
        client_advertised = [str(item) for item in
                             binding.get("requiredCapabilities", ())]
        required_satisfied = set(client_advertised) <= set(advertised)
        if bool(binding.get("requiredCapabilitiesSatisfied")) != required_satisfied:
            raise ServiceStartupError(
                "NETCONF capability satisfaction disagrees with the hello binding")
        session = {
            "endpointAuthority": str(binding["endpointAuthority"]),
            "hostKeyPinDigest": str(binding["hostKeyPinDigest"]),
            "hostKeyVerified": bool(binding.get("hostKeyVerified")),
            "clientAuthMethod": str(binding.get("clientAuthMethod", "")),
            "helloRaw": server_hello_raw,
            "clientHelloRaw": client_hello_raw,
            "advertisedCapabilities": advertised,
            "clientAdvertisedCapabilities": client_advertised,
            "requiredCapabilitiesSatisfied": required_satisfied,
            "negotiatedFraming": str(binding["negotiatedFraming"]),
            "wireEvidence": {
                "observationPoint": "NETCONF_SSH_SUBSYSTEM_PLAINTEXT_OCTETS",
                "sessionId": session_id,
                "clientToServerRaw": client_wire_raw,
                "serverToClientRaw": server_wire_raw,
                "clientToServerMessageCount": int(counters["clientMessages"]),
                "serverToClientMessageCount": int(counters["serverMessages"]),
                "clientToServerChunkCount": int(counters["clientChunks"]),
                "serverToClientChunkCount": int(counters["serverChunks"]),
                "clientToServerHelloEomCount": int(
                    counters["clientHelloEomCount"]),
                "serverToClientHelloEomCount": int(
                    counters["serverHelloEomCount"]),
                "clientToServerPostHelloEomCount": int(
                    counters["clientPostHelloEomCount"]),
                "serverToClientPostHelloEomCount": int(
                    counters["serverPostHelloEomCount"]),
            },
            "schemaMountVerified": bool(binding.get("schemaMountVerified")),
            "mountedYangLibraryDigest": str(
                binding.get("mountedYangLibraryDigest", "")),
            "yangClosureDigest": str(binding.get("yangClosureDigest", "")),
        }
        recorder.set_netconf_session(session)
        measured = dict(consumer.measured_segment_counters())
        start, end = measured.get("start"), measured.get("end")
        if start is None and end is None:
            setter(None)
            return
        if not isinstance(start, Mapping) or not isinstance(end, Mapping):
            raise ServiceStartupError("NETCONF measured boundary is incomplete")
        start_capture = self._netconf_counter_capture(start)
        end_capture = self._netconf_counter_capture(end)
        setter({
            "source": "NETCONF_SSH_SUBSYSTEM_TAP",
            "sessionId": session_id,
            "start": start_capture,
            "end": end_capture,
            "delta": {
                "measuredRpcDelta": (
                    end_capture["rpcRecordCount"] - start_capture["rpcRecordCount"]),
                "clientToServerOctetDelta": (
                    end_capture["clientToServerOctetCount"]
                    - start_capture["clientToServerOctetCount"]),
                "serverToClientOctetDelta": (
                    end_capture["serverToClientOctetCount"]
                    - start_capture["serverToClientOctetCount"]),
                "clientToServerMessageDelta": (
                    end_capture["clientToServerMessageCount"]
                    - start_capture["clientToServerMessageCount"]),
                "serverToClientMessageDelta": (
                    end_capture["serverToClientMessageCount"]
                    - start_capture["serverToClientMessageCount"]),
                "clientToServerChunkDelta": (
                    end_capture["clientToServerChunkCount"]
                    - start_capture["clientToServerChunkCount"]),
                "serverToClientChunkDelta": (
                    end_capture["serverToClientChunkCount"]
                    - start_capture["serverToClientChunkCount"]),
            },
        })

    @staticmethod
    def _netconf_counter_capture(snapshot: Mapping[str, Any]) -> dict[str, int]:
        wire = snapshot.get("wire")
        if not isinstance(wire, Mapping) or not wire.get("available"):
            raise ServiceStartupError("NETCONF measured boundary lacks wire counters")
        names = {
            "captureSequence": (snapshot, "captureSequence"),
            "monotonicNs": (snapshot, "monotonicNs"),
            "rpcRecordCount": (snapshot, "rpcCount"),
            "clientToServerOctetCount": (wire, "clientToServerOctets"),
            "serverToClientOctetCount": (wire, "serverToClientOctets"),
            "clientToServerMessageCount": (wire, "clientMessages"),
            "serverToClientMessageCount": (wire, "serverMessages"),
            "clientToServerChunkCount": (wire, "clientChunks"),
            "serverToClientChunkCount": (wire, "serverChunks"),
        }
        missing = [target for target, (source, key) in names.items()
                   if key not in source]
        if missing:
            raise ServiceStartupError(
                "NETCONF measured boundary lacks counters: %s"
                % ", ".join(missing))
        return {target: int(source[key])
                for target, (source, key) in names.items()}

    def _declared_secret_references(self) -> list[str]:
        references: list[str] = []
        for section, keys in (
                (("security",), ("truststoreRef", "r1ClientCredentialRef",
                                 "o1NotificationCredentialRef")),
                (("o1", "sftp"), ("knownHostsRef", "credentialRef")),
                (("o1", "netconf"), ("knownHostsRef", "credentialRef"))):
            node: Any = self.vector
            for token in section:
                node = node.get(token, {}) if isinstance(node, Mapping) else {}
            for key in keys:
                value = node.get(key) if isinstance(node, Mapping) else None
                if isinstance(value, str) and "://" in value:
                    references.append(value)
        return sorted(set(references))

    def _state_snapshot(self) -> dict[str, Any]:
        dme = self.dme.observations()
        o1_state = ({} if self.o1_consumer is None
                    else dict(self.o1_consumer.harness_state()))
        return state_snapshot(
            r1_counts=self.r1.state_counts(),
            binding_count=int(dme["deliveryBindingCount"]),
            evidence_commit_count=int(dme["evidenceCommitCount"]),
            retrieved_file_count=int(o1_state.get("retrievedFileCount", 0)),
            perf_metric_job_present=(
                bool(o1_state.get("perfMetricJobPresent"))
                if self._gate.netconf_adapter_actions else None))

    def _record_applied_rules(self, recorder: CaptureRecorder) -> None:
        """One entry per rule the CATALOG declares -- no more, no fewer.

        ``RULE-O1-POSITIONAL-MAP`` is deliberately absent for SC-084: a rule the
        catalog does not declare is a fabricated assertion, and a rule it
        declares but the harness omits is a silent gap.

        The observations are the values the rule ranges over, ECHOED from the
        primary slots that already carry them.  Echoing rather than re-deriving
        is what keeps a two-run comparison honest: the live values are volatile
        by design, and a second independently produced copy of them would be a
        second source of truth for the same observation.
        """
        document = recorder.snapshot()
        state = document["state"]["after"]
        dme = self.dme.observations()
        echoes = _rule_echoes(document)
        for rule_id in self.plan.rules:
            observations: dict[str, Any] = {
                "policyCount": state["policyCount"],
                "dmeDataJobCount": state["dmeDataJobCount"],
                "evidenceCommitCount": state["evidenceCommitCount"],
                "acceptedPushes": int(dme["acceptedPushes"]),
            }
            observations.update(echoes.get(rule_id, {}))
            recorder.add_applied_rule(
                rule_id, inputs=[self.plan.rules_pointer],
                observations=observations)

    # -- properties -------------------------------------------------------
    @property
    def endpoints(self) -> Lo1Endpoints:
        return self._endpoints

    @property
    def recorder(self) -> CaptureRecorder:
        recorder = self._recorder
        if recorder is None:
            raise ServiceStartupError("no scenario has been reset for capture")
        return recorder

    @property
    def gate(self) -> GateResult:
        return self._gate


class _RecorderProxy:
    """Late-bound recorder view, so components built at start can record later.

    It forwards the whole frozen capture seam, not a subset: a component handed
    a proxy that silently lacked ``set_netconf_assignment`` or ``raw_store``
    would fail at its first write, which is exactly the integration fault this
    class exists to remove.  Writes made before ``reset`` mints the recorder are
    dropped rather than buffered, because ``reset`` re-establishes every one of
    them from the gate's own observations.
    """

    def __init__(self, service: "UpperLiveO1HarnessService") -> None:
        self._service = service

    @property
    def clock(self) -> ObservedClock:
        return self._service.clock

    @property
    def raw_store(self) -> RawStore:
        """The run's raw store exists from construction, not from ``reset``."""
        return self._service.raw_store

    @property
    def egress_guard(self) -> Any:
        """The running guard, so an SSH client cannot run unobserved."""
        return self._service.egress

    def _recorder(self) -> CaptureRecorder | None:
        return self._service._recorder  # noqa: SLF001 - same package seam

    def next_sequence(self) -> int:
        recorder = self._recorder()
        return recorder.next_sequence() if recorder is not None else 0

    def emit_harness_operation(self, record: Mapping[str, Any]) -> int:
        recorder = self._recorder()
        return recorder.emit_harness_operation(record) if recorder is not None else 0

    def emit_notification(self, record: Mapping[str, Any]) -> int:
        recorder = self._recorder()
        return recorder.emit_notification(record) if recorder is not None else 0

    def emit_retrieval(self, record: Mapping[str, Any]) -> int:
        recorder = self._recorder()
        return recorder.emit_retrieval(record) if recorder is not None else 0

    def emit_netconf_rpc(self, record: Mapping[str, Any]) -> int:
        recorder = self._recorder()
        return recorder.emit_netconf_rpc(record) if recorder is not None else 0

    def emit_cleanup_action(self, record: Mapping[str, Any]) -> int:
        recorder = self._recorder()
        return recorder.emit_cleanup_action(record) if recorder is not None else 0

    def set_coordinator(self, observations: Mapping[str, Any]) -> None:
        recorder = self._recorder()
        if recorder is not None:
            recorder.set_coordinator(observations)

    def set_deterministic_stubs(self, observations: Mapping[str, Any]) -> None:
        recorder = self._recorder()
        if recorder is not None:
            recorder.set_deterministic_stubs(observations)

    def set_netconf_assignment(self, assignment: Mapping[str, Any]) -> None:
        recorder = self._recorder()
        if recorder is not None:
            recorder.set_netconf_assignment(assignment)

    def set_o1_readiness(self, readiness: Mapping[str, Any] | None) -> None:
        recorder = self._recorder()
        if recorder is not None:
            recorder.set_o1_readiness(readiness)

    def set_netconf_session(self, session: Mapping[str, Any] | None) -> None:
        recorder = self._recorder()
        if recorder is not None:
            recorder.set_netconf_session(session)

    def set_perf_metric_job(self, job: Mapping[str, Any] | None) -> None:
        recorder = self._recorder()
        if recorder is not None:
            recorder.set_perf_metric_job(job)

    def set_normalization(self, normalization: Mapping[str, Any]) -> None:
        recorder = self._recorder()
        if recorder is not None:
            recorder.set_normalization(normalization)

    def set_cleanup_path(self, *, path: str, complete: bool,
                         residual: Sequence[str]) -> None:
        recorder = self._recorder()
        if recorder is not None:
            recorder.set_cleanup_path(path=path, complete=complete,
                                      residual=residual)

    def add_applied_rule(self, rule_id: str, *, inputs: Sequence[str],
                         observations: Mapping[str, Any]) -> None:
        recorder = self._recorder()
        if recorder is not None:
            recorder.add_applied_rule(rule_id, inputs=inputs,
                                      observations=observations)

    def note_secret_reference(self, reference: str) -> None:
        recorder = self._recorder()
        if recorder is not None:
            recorder.note_secret_reference(reference)


class _O1ConsumerBinding:
    """Binds W1's O1 consumer to the integration control surface.

    ``o1_consumer.py``'s public shape is frozen, so the harness-operation
    vocabulary the control surface dispatches lives here instead of being welded
    into the consumer.  Every operation this exposes is one the role table marks
    assignable; nothing here can enable an action the gate did not assign.
    """

    def __init__(self, *, consumer: Any) -> None:
        self.consumer = consumer
        self._latest_retrieved: Any = None
        #: READ from the role table: exactly the rows it marks
        #: REQUIRED_READINESS_ASSIGNED.  A fourth operation cannot appear here
        #: without appearing in the frozen table first.
        self.adapter_ops = frozenset(readiness_operations(load_role_table()))

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        self.consumer.start()

    def cleanup(self, *, failure: bool) -> list[dict[str, Any]]:
        return list(self.consumer.cleanup(failure=failure))

    def stop(self) -> None:
        self.consumer.stop()

    def residual(self) -> list[str]:
        netconf = getattr(self.consumer, "netconf", None)
        if netconf is None or not hasattr(netconf, "residual"):
            return []
        return [str(item) for item in netconf.residual()]

    def readiness_capture(self) -> dict[str, Any] | None:
        readiness = getattr(self.consumer, "readiness", None)
        return None if readiness is None else readiness.as_capture()

    def netconf_session_binding(self) -> dict[str, Any]:
        return dict(self.consumer.netconf_session_binding())

    def netconf_wire_evidence(self) -> dict[str, Any]:
        return dict(self.consumer.netconf_wire_evidence())

    def mark_measured_segment_end(self) -> dict[str, Any]:
        return dict(self.consumer.mark_measured_segment_end())

    def measured_segment_counters(self) -> dict[str, Any]:
        return dict(self.consumer.measured_segment_counters())

    # -- inbound ----------------------------------------------------------
    def accept_notification(self, *, headers: Mapping[str, str], raw: bytes,
                            peer: str) -> tuple[int, dict[str, Any]]:
        return self.consumer.accept_notification(headers=headers, raw=raw,
                                                 peer=peer)

    # -- control surface --------------------------------------------------
    def harness_op(self, request: Mapping[str, Any]) -> dict[str, Any]:
        arguments = dict(request)
        op = str(arguments.pop("op", ""))
        if op not in self.adapter_ops:
            raise ServiceStartupError(
                "the O1 consumer serves no operation %s" % op)
        return {"outputs": dict(self.consumer.perf_metric_job(action=op))}

    def retrieve_latest(self) -> dict[str, Any]:
        state = self.consumer.journal._read()  # noqa: SLF001 - same durable seam
        accepted = list(state.get("acceptedNotifications", ()))
        if len(accepted) != 1:
            raise ServiceStartupError(
                "O1_RETRIEVE requires exactly one durable notification")
        files = list(accepted[0].get("document", {}).get("fileInfoList", ()))
        if len(files) != 1:
            raise ServiceStartupError(
                "O1_RETRIEVE requires exactly one notified PM file")
        retrieved = self.consumer.retrieve(file_info=files[0])
        self._latest_retrieved = retrieved
        return {
            "byteCount": int(retrieved.byte_count),
            "byteSha256": str(retrieved.raw_sha256),
            "rawArtifactPath": str(retrieved.artifact_path),
            "fileInfo": dict(retrieved.file_info),
        }

    def normalize_latest(self, *, context: Mapping[str, Any]
                         ) -> list[dict[str, Any]]:
        if self._latest_retrieved is None:
            raise ServiceStartupError(
                "O1_NORMALIZE requires a successful O1_RETRIEVE")
        normalized_context = dict(context)
        normalized_context.setdefault(
            "evaluationNow",
            str(self._latest_retrieved.file_info["_retrievedAt"]))
        return list(self.consumer.normalize(
            retrieved=self._latest_retrieved, context=normalized_context))

    def harness_state(self) -> dict[str, Any]:
        netconf = getattr(self.consumer, "netconf", None)
        assigned = bool(getattr(netconf, "enabled", False))
        state: dict[str, Any] = {
            "bound": True,
            "notificationsAccepted": int(getattr(
                self.consumer, "_notifications_accepted", 0)),
            "retrievedFileCount": len(
                getattr(self.consumer, "_retrieved_files", ()) or ()),
            "normalizationInvocations": int(getattr(
                self.consumer, "_normalization_invocations", 0)),
            "perfMetricJobPresent": None,
        }
        if netconf is not None and hasattr(netconf, "state"):
            state.update(netconf.state())
        if assigned:
            evidence = netconf.lifecycle_evidence()
            state["perfMetricJobPresent"] = any(
                item.get("step") == "get-perfmetricjob.xml"
                for item in evidence.get("readiness", ()))
            state["netconfLifecycleSegments"] = {
                name: len(entries) for name, entries in evidence.items()}
        readiness = getattr(self.consumer, "readiness", None)
        if readiness is not None:
            state["readinessComplete"] = bool(readiness.complete)
            state["measuredBodyAdmitted"] = bool(readiness.measured_body_admitted)
            state["readinessUnmet"] = list(readiness.unmet())
        return state


def run_gate_under_guard(config: Lo1StartupConfig) -> tuple[GateResult, dict[str, Any]]:
    """Run the gate with an EMPTY allowlist armed, and measure the result.

    G-GATE-1 asks for ``connectionAttempts == 0`` at the instant the gate
    returns, on every path including refusal.  Arming a guard whose allowlist is
    empty makes that a measurement: any connect during the gate is refused and
    counted, so a zero here cannot be an unmeasured claim.
    """
    guard = EgressGuard(allowlist=(), hardware_source="pre-gate guard")
    guard.install()
    try:
        result = run_identity_authority_gate(
            startup=config, spec_path=_gate_spec_path(config))
    except GateRefused as refusal:
        observed = guard.observations(scope="scenario")
        refusal.connection_attempts = int(observed["connectionAttempts"])  # type: ignore[attr-defined]
        raise
    finally:
        guard.uninstall()
    return result, guard.observations(scope="scenario")


def _gate_spec_path(config: Lo1StartupConfig) -> Path:
    for root in (config.spec_dir,
                 Path(__file__).resolve().parents[3] / "docs" / "upper-live-o1-harness"):
        candidate = Path(root) / GATE_SPEC_FILE_NAME
        if candidate.is_file():
            return candidate
    raise ServiceStartupError("the frozen %s was not found" % GATE_SPEC_FILE_NAME)


#: Which primary slot each echoed observation name quotes.  The names are the
#: ones the self-test's determinism declaration excuses as echoes; anything not
#: listed here stays under byte-for-byte comparison between two runs.
_RULE_ECHO_SOURCES = {
    "RULE-O1-TIME-RELATIONS": ("windowStarts", "windowEnds", "measurementAgeMs",
                               "ingestLatencyMs"),
    "RULE-O1-LIVE-VALUE-INVARIANTS": ("values",),
    "RULE-O1-FILE-TEMPORAL-ORDER": ("eventTimes", "fileReadyTimes",
                                    "fileExpirationTimes", "retrievalCompletedAt"),
    "RULE-O1-RAW-DIGEST-SELF-CONSISTENT": ("retrievalDigests",
                                           "recordSourceDigests"),
}


def _rule_echoes(document: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Collect each echoed observation from the primary slot that holds it."""
    records = list(document.get("normalization", {}).get("records", []))
    notifications = list(document.get("notifications", []))
    retrievals = list(document.get("retrievals", []))
    available = {
        "windowStarts": [item["window"]["start"] for item in records],
        "windowEnds": [item["window"]["end"] for item in records],
        "measurementAgeMs": [item["measurementAgeMs"] for item in records],
        "ingestLatencyMs": [item["ingestLatencyMs"] for item in records],
        "values": [item["value"] for item in records],
        "recordSourceDigests": [item["sourceFileSha256"] for item in records],
        "eventTimes": [item["eventTime"] for item in notifications
                       if "eventTime" in item],
        "fileReadyTimes": [info["fileReadyTime"] for item in notifications
                           for info in item.get("fileInfoList", [])],
        "fileExpirationTimes": [info["fileExpirationTime"] for item in notifications
                                for info in item.get("fileInfoList", [])],
        "retrievalCompletedAt": [item["completedAt"] for item in retrievals],
        "retrievalDigests": [item["byteSha256"] for item in retrievals],
    }
    return {rule: {name: available[name] for name in names}
            for rule, names in _RULE_ECHO_SOURCES.items()}


def _json(body: bytes) -> dict[str, Any] | None:
    if not body:
        return None
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _dump(payload: Mapping[str, Any] | None) -> bytes:
    return json.dumps(payload or {}, sort_keys=True, separators=(",", ":")).encode(
        "utf-8")
