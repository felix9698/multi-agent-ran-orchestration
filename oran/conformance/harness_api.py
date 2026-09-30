"""Development-only black-box harness management-plane clients.

This module deliberately owns *clients*, never target component implementation.
The endpoints are not O-RAN interfaces and must be enabled by the component only
for a loopback insecure development profile.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any, Mapping
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class HarnessError(RuntimeError):
    """A harness boundary did not honour the shared development protocol."""


class HarnessAdapter(ABC):
    """Client contract for POST /harness/{op,fault,restart} and GET state."""

    @abstractmethod
    def operation(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Run a contract operation and return its declared ``outputs``."""

    @abstractmethod
    def schedule_fault(self, fault: str, boundary: Mapping[str, Any]) -> None:
        """Schedule one contract fault at its declared boundary."""

    @abstractmethod
    def restart(self, component: str) -> dict[str, Any]:
        """Restart from durable state only."""

    @abstractmethod
    def state(self) -> dict[str, Any]:
        """Return only externally observable state."""


@dataclass(frozen=True)
class HttpHarnessAdapter(HarnessAdapter):
    """Stdlib HTTP client for one component's development management plane."""

    base_url: str
    timeout_seconds: float = 10.0

    def _request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(
            self.base_url.rstrip("/") + path,
            data=data,
            method=method,
            headers={"Accept": "application/json", **({"Content-Type": "application/json"} if data else {})},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # nosec B310: endpoint is vector-configured
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise HarnessError("harness request failed: %s: %s" % (exc, detail)) from exc
        except (URLError, OSError) as exc:
            raise HarnessError("harness request failed: %s" % exc) from exc
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HarnessError("harness response is not JSON") from exc
        if not isinstance(body, dict):
            raise HarnessError("harness response must be a JSON object")
        return body

    def operation(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        body = self._request("POST", "/harness/op", {"op": name, **dict(arguments)})
        outputs = body.get("outputs")
        if not isinstance(outputs, dict):
            raise HarnessError("harness operation response has no outputs object")
        return outputs

    def schedule_fault(self, fault: str, boundary: Mapping[str, Any]) -> None:
        self._request("POST", "/harness/fault", {"fault": fault, "boundary": dict(boundary)})

    def restart(self, component: str) -> dict[str, Any]:
        body = self._request("POST", "/harness/restart", {"component": component})
        outputs = body.get("outputs", body)
        if not isinstance(outputs, dict):
            raise HarnessError("harness restart response must be a JSON object")
        return outputs

    def state(self) -> dict[str, Any]:
        return self._request("GET", "/harness/state")


class _PersonaAdapter(HarnessAdapter):
    """Named routing adapter; persona names make suite ownership explicit."""

    persona: str

    def __init__(self, client: HarnessAdapter):
        self._client = client

    def operation(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        return self._client.operation(name, arguments)

    def schedule_fault(self, fault: str, boundary: Mapping[str, Any]) -> None:
        self._client.schedule_fault(fault, boundary)

    def restart(self, component: str) -> dict[str, Any]:
        return self._client.restart(component)

    def state(self) -> dict[str, Any]:
        return self._client.state()


class RappR1Harness(_PersonaAdapter):
    persona = "RAPP_R1_HARNESS"


class A1ConsumerBlackBoxHarness(_PersonaAdapter):
    persona = "A1_CONSUMER_BLACK_BOX_HARNESS"


class O1ConsumerHarness(_PersonaAdapter):
    persona = "O1_CONSUMER_HARNESS"


class O1ProviderHarness(_PersonaAdapter):
    persona = "O1_PROVIDER_AND_DME_HARNESS"


class R1A1E2O1BlackBoxHarness(_PersonaAdapter):
    persona = "R1_A1_E2_O1_BLACK_BOX_HARNESS"


class ReciprocalBlackBoxHarness(_PersonaAdapter):
    persona = "RECIPROCAL_BLACK_BOX_HARNESS"


def _origin(uri: str, label: str) -> str:
    parsed = urlsplit(uri)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.port is None:
        raise HarnessError("deployment vector endpoint has no explicit origin: %s" % label)
    host = "[%s]" % parsed.hostname if ":" in parsed.hostname else parsed.hostname
    return "%s://%s:%s" % (parsed.scheme, host, parsed.port)


class RoutedHarnessAdapter(HarnessAdapter):
    """Route runner operations to the owning component's real harness surface.

    Origins are derived solely from existing deployment-vector endpoints.  The
    mapping is contract ownership, not a substitute component implementation.
    """

    NEARRT_OPERATIONS = frozenset({
        "A1_SEED_RESOURCE", "A1_EMIT_STATUS", "KPM_SNAPSHOT", "E2_CONTROL_RESULT", "READBACK",
        "SET_DEPENDENCY", "DECISION_RESULT", "E2_STUB_LOG",
        "E2_INVENTORY_VALIDATE", "A1_INSTALL_POLICY_TYPE", "LOAD_CAPABILITY",
        "SET_DEPENDENCIES", "SET_RAN_STATE", "INSTALL_STATUS_DESTINATION",
    })
    NONRT_OPERATIONS = frozenset({
        "START_R1_A1_SERVICE", "INSTALL_R1_STATUS_SUBSCRIPTION",
        "INSTALL_DME_REGISTRATION", "INSTALL_ACTIVE_DATA_JOB",
        "START_SERVICE_REGISTRATION_API", "INSTALL_PINNED_SERVICE_DESCRIPTIONS",
        "START_R1_DME_APIS", "INSTALL_NON_RT_DESIRED_POLICY",
        "CONFIGURE_NEXT_POLICY_ID",
    })
    O1_PROVIDER_OPERATIONS = frozenset({
        "INSTALL_PROVIDER_SUBSCRIPTION", "DELETE_PROVIDER_SUBSCRIPTION_IF_PRESENT",
        "SELECT_SECURITY_FIXTURE", "CONFIGURE_LIVE_PM_PROFILE",
    })
    O1_CONSUMER_OPERATIONS = frozenset({
        "LOAD_O1_PROFILE", "INSTALL_O1_SUBSCRIPTION", "LOAD_LIVE_CELL_MAPPING",
        "SET_PERF_METRIC_JOB", "CLEAR_SUBSCRIPTION_LEDGER",
        "LOAD_SUBSCRIPTION_LEDGER", "LOAD_UNCERTAIN_SUBSCRIPTION_LEDGER",
        "O1_NORMALIZE", "O1_VALIDATE_RETRIEVED",
    })

    def __init__(self, suite: str, clients: Mapping[str, HarnessAdapter]):
        self.suite = suite
        self.clients = dict(clients)
        self._captured_http_indexes: set[int] = set()

    @classmethod
    def from_vector(cls, suite: str, vector: Mapping[str, Any]) -> "RoutedHarnessAdapter":
        roots = {
            "nonrt": vector["r1"]["apiRoot"],
            "nearrt": vector["a1"]["apiRoot"],
            "rapp": vector["r1"]["callbackApi"]["rootUri"],
            "o1_provider": vector["o1"]["fileDataReporting"]["mnsRoot"],
            "o1_consumer": vector["o1"]["fileDataReporting"]["consumerReference"],
        }
        return cls(suite, {
            name: HttpHarnessAdapter(_origin(uri, name)) for name, uri in roots.items()
        })

    def _owner(self, operation: str) -> str:
        if operation == "SELECT_SECURITY_FIXTURE":
            return self._target_component()
        if operation == "SET_DEPENDENCY" and self.suite == "o1-lifecycle-contract":
            return "o1_consumer"
        if operation in self.NEARRT_OPERATIONS:
            return "nearrt"
        if operation in self.NONRT_OPERATIONS:
            return "nonrt"
        if operation in self.O1_PROVIDER_OPERATIONS:
            return "o1_provider"
        if operation in self.O1_CONSUMER_OPERATIONS:
            return "o1_consumer"
        if operation in {"COORDINATOR_TRANSITION", "COORDINATOR_PROCESS_INTENT"}:
            return "rapp"
        if operation in {"R1_DME_BINDING_PRECREATE", "R1_DME_BINDING_COMMIT"}:
            return "rapp"
        if operation == "RESET_SCENARIO_STATE":
            return self._target_component()
        raise HarnessError("no component owns harness operation: %s" % operation)

    def _target_component(self) -> str:
        return {
            "r1-service-conformance": "nonrt",
            "a1p-producer-conformance": "nearrt",
            "o1-consumer-normalizer-conformance": "o1_consumer",
            "o1-provider-profile-conformance": "o1_provider",
            "o1-lifecycle-contract": "o1_consumer",
            "end-to-end-contract": "nearrt",
        }[self.suite]

    def operation(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name == "RESET_SCENARIO_STATE":
            self._captured_http_indexes.clear()
            for client in self.clients.values():
                client.operation(name, arguments)
            return {"resetCompleted": True}
        if name in {"LOAD_SUBSCRIPTION_LEDGER", "LOAD_UNCERTAIN_SUBSCRIPTION_LEDGER"}:
            outputs = self.clients["o1_consumer"].operation(name, arguments)
            # The default setup represents a provider resource that may still
            # exist.  PROVIDER_HAS_NO_SUCH_SUBSCRIPTION, when declared later in
            # the same initial-state list, removes it deterministically.
            self.clients["o1_provider"].operation(
                "INSTALL_PROVIDER_SUBSCRIPTION", arguments)
            return outputs
        if name == "INSTALL_ACTIVE_DATA_JOB":
            outputs = self.clients["nonrt"].operation(name, arguments)
            self.clients["rapp"].operation("R1_DME_BINDING_PRECREATE", {
                "deliveryBindingId": arguments["deliveryBindingId"],
                "job": arguments["job"],
            })
            self.clients["rapp"].operation("R1_DME_BINDING_COMMIT", {
                "deliveryBindingId": arguments["deliveryBindingId"],
                "dataJobId": arguments["dataJobId"],
            })
            return outputs
        if name == "LOAD_CAPABILITY":
            # The same digest-pinned capability is consumed independently by
            # Near-RT admission and O1 correlation/normalization.
            effective = dict(arguments)
            if "capability" not in effective and isinstance(effective.get("manifest"), Mapping):
                effective["capability"] = effective["manifest"]
            self.clients["nearrt"].operation(name, effective)
            self.clients["o1_consumer"].operation(name, effective)
            return {"ready": True}
        if name == "LOAD_LIVE_CELL_MAPPING":
            return self.clients["o1_consumer"].operation(name, arguments)
        if name == "A1_SEED_RESOURCE":
            # The scenario seed represents one already reconciled A1 resource;
            # mirror that durable desired resource into its R1 owner without
            # creating an additional A1 write.
            outputs = self.clients["nearrt"].operation(name, arguments)
            self.clients["nonrt"].operation("INSTALL_NON_RT_DESIRED_POLICY", arguments)
            return outputs
        return self.clients[self._owner(name)].operation(name, arguments)

    def captured_http(self, method: str, endpoint: str, body: Any) -> dict[str, Any]:
        """Return an HTTP exchange already emitted by a real counterpart.

        CAPTURE_AND_RESPOND steps describe observation of the Non-RT consumer's
        A1 call, not a second request from the runner.  The exchange is read
        from the Near-RT harness trace and consumed once.
        """
        path = urlsplit(endpoint).path
        interactions = self.clients["nearrt"].state().get("httpInteractions", [])
        for index, item in enumerate(interactions):
            if (index not in self._captured_http_indexes
                    and item.get("method") == method
                    and item.get("path") == path
                    and item.get("requestBody") == body):
                self._captured_http_indexes.add(index)
                response = item.get("response")
                if isinstance(response, dict) and isinstance(response.get("status"), int):
                    return response
        raise HarnessError("counterpart HTTP exchange was not observed: %s %s" % (method, path))

    def schedule_fault(self, fault: str, boundary: Mapping[str, Any]) -> None:
        if fault in {"FLIP_RETRIEVED_BYTE", "DROP_O1_NOTIFICATION", "TLS_HANDSHAKE_REJECT"}:
            owner = "o1_provider"
        elif fault == "DROP_CALLBACK_DELIVERY" and (
                self.suite == "a1p-producer-conformance" or "afterStep" in boundary):
            owner = "nearrt"
        else:
            owner = "nonrt"
        effective = dict(boundary)
        if owner == "nonrt" and fault == "DROP_HTTP_RESPONSE":
            effective = {"stage": "AFTER_R1_CREATE_COMMIT"}
        elif owner == "nonrt" and fault == "DROP_CALLBACK_DELIVERY":
            effective = {"sender": "NON_RT_RIC_FRAMEWORK", "interface": "R1_STATUS"}
        elif owner == "nonrt" and fault == "CRASH_PROCESS" and isinstance(
                boundary.get("boundary"), str):
            effective = {"stage": boundary["boundary"]}
        self.clients[owner].schedule_fault(fault, effective)

    def restart(self, component: str) -> dict[str, Any]:
        if component.startswith("NON_RT"):
            owner = "nonrt"
        elif component.startswith("NEAR_RT"):
            owner = "nearrt"
        elif component == "O1_PROVIDER":
            owner = "o1_provider"
        elif component == "O1_CONSUMER":
            owner = "o1_consumer"
        else:
            raise HarnessError("unknown restart component: %s" % component)
        return self.clients[owner].restart(component)

    def state(self) -> dict[str, Any]:
        # The target's own observations are authoritative for suite oracles.
        state = self.clients[self._target_component()].state()
        if self._target_component() != "nonrt":
            nonrt = self.clients["nonrt"].state()
            if "acceptedPushPayloadDigests" in nonrt:
                state["acceptedPushPayloadDigests"] = deepcopy(
                    nonrt["acceptedPushPayloadDigests"])
        if self.suite == "r1-service-conformance":
            nearrt = self.clients["nearrt"].state()
            for key, value in nearrt.items():
                if key in {
                        "a1PolicyResource", "statusBody", "enforceStatus",
                        "policyState", "policyTerminal", "episodeState",
                        "episodeTerminal", "errorCode", "e2ControlAttempts",
                        "assertionRules", "statusHistory",
                }:
                    state.setdefault(key, deepcopy(value))
        if self.suite == "o1-lifecycle-contract":
            provider = self.clients["o1_provider"].state()
            state.update({key: value for key, value in provider.items()
                          if key in {"subscriptionPostCount", "subscriptionDeleteCount",
                                     "deleteAlreadyAbsentConverged"}})
            if state.get("subscriptionResource") != "UNKNOWN":
                state["subscriptionResource"] = provider.get("subscriptionResource")
        if self.suite == "end-to-end-contract":
            rapp = self.clients["rapp"].state()
            state.update({key: value for key, value in rapp.items()
                          if key != "transitions" or key not in state})
        return state


class LocalFakeHarness(HarnessAdapter):
    """Hermetic adapter used solely to verify runner dispatch and capture logic."""

    def __init__(self, outputs: Mapping[str, Mapping[str, Any]] | None = None,
                 observable_state: Mapping[str, Any] | None = None):
        self.outputs = {key: dict(value) for key, value in (outputs or {}).items()}
        self.observable_state = dict(observable_state or {})
        self.calls: list[tuple[str, Any]] = []

    def operation(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(("op", name, dict(arguments)))
        return dict(self.outputs.get(name, {}))

    def schedule_fault(self, fault: str, boundary: Mapping[str, Any]) -> None:
        self.calls.append(("fault", fault, dict(boundary)))

    def restart(self, component: str) -> dict[str, Any]:
        self.calls.append(("restart", component))
        return {"restartCompleted": True}

    def state(self) -> dict[str, Any]:
        self.calls.append(("state",))
        return dict(self.observable_state)
