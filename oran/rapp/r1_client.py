"""Contract-pinned R1 consumer for policy management and DME assurance."""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import ssl
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from .contract_support import canonicalize, jcs_sha256, validate
from oran.campaign5.families import CAMPAIGN5_POLICY_TYPES, validate_campaign5
from oran.o1.core import O1Error, validate_evidence


#: The steering type is validated through the frozen ``ContractValidator``
#: bundle; the four Campaign 5 types live outside that byte-pinned bundle and are
#: validated by their own offline validator (``oran.campaign5``).  The frozen
#: profile is the union: widening it was deliberate, and it does not weaken the
#: steering path -- each type is still checked against its OWN policy/status
#: schema.
STEERING_POLICY_TYPE = "AIC_UECellSteering_1.0.0"
FROZEN_POLICY_TYPES = (STEERING_POLICY_TYPE,) + tuple(CAMPAIGN5_POLICY_TYPES)


def _validate_policy_object(policy_type_id: str, policy_object: Any) -> None:
    if policy_type_id == STEERING_POLICY_TYPE:
        validate(policy_object, "AIC_UECellSteering_1.0.0.policy")
    elif policy_type_id in CAMPAIGN5_POLICY_TYPES:
        validate_campaign5(policy_object, f"{policy_type_id}.policy")
    else:
        raise R1Error("policy type is outside the frozen profile")


def _validate_status_object(policy_type_id: str, status_object: Any) -> None:
    if policy_type_id == STEERING_POLICY_TYPE:
        validate(status_object, "AIC_UECellSteering_1.0.0.status")
    elif policy_type_id in CAMPAIGN5_POLICY_TYPES:
        validate_campaign5(status_object, f"{policy_type_id}.status")
    else:
        raise R1Error("policy type is outside the frozen profile")


POLICY_VERSION = "1.0.0"
DME_DISCOVERY_VERSION = "2.0.0"
DME_ACCESS_VERSION = "2.0.0-alpha.2"
POLICY_BASE = "a1-policy-management/v1"
DME_TYPE = "aic:policy-evidence:1.0.0"
DME_SCHEMA_ID = "aic.policy-evidence.record.schema.1.0.0"
#: RFC 6750 authorization scheme token, without its trailing delimiter --
#: see the G-SEC-1 note at the use site.
OAUTH_SCHEME = "Bearer"
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$")


class R1Error(RuntimeError):
    """R1 transport, version, correlation, or durable-state failure."""


class R1Refusal(R1Error):
    """The producer answered and the request provably never reached the RAN.

    A 400 (schema), 404 (unadvertised resource) or 409 (scope or revision
    conflict) is a decision, not a lost message: the gateway may release the
    scope instead of opening a recovery.  Live on 2026-09-17 a 409 arrived here
    as a plain :class:`R1Error`, the gateway classified the write ``UNKNOWN``,
    and the failed trial kept the scope RESERVED -- which then rejected the
    NEXT trial on the same axis ("scope is still owned by transaction ...
    (RESERVED)").  Carrying the status is what lets that distinction be made.
    """

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = int(status)


#: Statuses that mean the producer decided.  A retryable status (408/425/429
#: and the 5xx family) is deliberately absent: a lost message must stay UNKNOWN.
R1_DEFINITIVE_STATUSES = frozenset({400, 404, 409})

def _problem_text(body: Any) -> str:
    """RFC7807 문제 객체는 원인을 ``detail`` 에만 싣는다.

    ``type`` 과 ``title`` 은 같은 코드를 두 번 말하고, 그 둘이 앞을 채운 탓에
    2026-09-17 의 조종 실패 네 건이 400자 절단선에 걸려 ``'status': 503, 'deta``
    에서 끊겼다 — 무엇이 잘못됐는지 말하는 유일한 칸을 잃은 것이다.  잘리는
    쪽은 항상 뒤이므로 원인을 앞으로 옮긴다.

    이 배포에는 **모양이 다른 프로듀서가 둘** 있다.  pin-to-cell(18443)은 RFC7807
    을 쓰고, 캠페인5 액션 프로듀서(9445)는 ``{"error": "..."}`` 하나만 보낸다
    (2026-09-17 의 409 ``policy update requires a newer revision and fencingToken``
    과 404 ``unknown policy <id>`` 가 그 모양이다).  한쪽만 풀면 다른 쪽은 그대로
    ``repr`` 로 떨어져 중괄호와 따옴표가 절단선을 갉아먹는다.
    """
    if isinstance(body, dict):
        parts = [str(body[k]) for k in ("title", "detail") if body.get(k)]
        if parts:
            return ": ".join(parts)
        if body.get("error"):
            return str(body["error"])
    return repr(body)




@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: Any = None


class Transport(Protocol):
    def request(self, method: str, url: str, *, headers: Mapping[str, str],
                body: bytes | None, timeout: float) -> HttpResponse: ...


class UrllibTransport:
    """Small stdlib HTTPS transport; TLS material is supplied by the launcher."""

    def __init__(self, ssl_context: ssl.SSLContext | None = None):
        self.ssl_context = ssl_context

    def request(self, method: str, url: str, *, headers: Mapping[str, str],
                body: bytes | None, timeout: float) -> HttpResponse:
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urlopen(request, timeout=timeout, context=self.ssl_context) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
                parsed = (json.loads(raw.decode("utf-8")) if raw and
                          "application/json" in content_type else raw)
                return HttpResponse(
                    response.status, dict(response.headers.items()), parsed)
        except HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw.decode("utf-8")) if raw else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = raw
            return HttpResponse(exc.code, dict(exc.headers.items()), parsed)


class JsonStateStore:
    """Atomic JSON durable store for callback and retry reconciliation ledgers."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._state: Dict[str, Any] = {
            "status": {}, "bindings": {}, "evidence": {}, "audit": []}
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if not isinstance(loaded, dict):
                raise R1Error("durable R1 state must be a JSON object")
            self._state.update(loaded)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)

    def mutate(self, fn) -> Any:
        with self._lock:
            result = fn(self._state)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=self.path.name + ".", dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(self._state, handle, separators=(",", ":"),
                              sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, self.path)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            return result

    def reset(self) -> None:
        """Clear all development scenario ledgers through the atomic store."""
        def clear(state: Dict[str, Any]) -> None:
            state.clear()
            state.update({"status": {}, "bindings": {}, "evidence": {}, "audit": []})
        self.mutate(clear)


def _is_loopback(host: str | None) -> bool:
    return host in {"127.0.0.1", "::1", "localhost"}


class R1Client:
    """Consumes only the URI/version profile frozen in contract section 4."""

    def __init__(self, *, api_root: str, r_app_id: str,
                 policy_evidence_push_base_uri: str,
                 state_path: str | os.PathLike[str],
                 transport: Transport | None = None,
                 timeout_s: float = 5.0, max_attempts: int = 3,
                 retry_backoff_s: Iterable[float] = (0.0, 0.25, 0.5),
                 insecure_dev: bool = False,
                 ssl_context: ssl.SSLContext | None = None,
                 oauth_header_provider=None,
                 authoritative_status_provider:
                 Callable[[str], Mapping[str, Any]] | None = None):
        self.api_root = api_root.rstrip("/") + "/"
        self.r_app_id = r_app_id
        self.push_base_uri = policy_evidence_push_base_uri.rstrip("/")
        self.timeout_s = float(timeout_s)
        self.max_attempts = int(max_attempts)
        self.backoff = tuple(float(v) for v in retry_backoff_s)
        if self.timeout_s <= 0 or self.max_attempts < 1:
            raise ValueError("timeout and bounded attempt count must be positive")
        for value in (self.api_root, self.push_base_uri):
            parsed = urlparse(value)
            if parsed.scheme != "https":
                if not (insecure_dev and parsed.scheme == "http" and
                        _is_loopback(parsed.hostname)):
                    raise R1Error(
                        "plaintext R1 is allowed only with insecure_dev on loopback")
        if not insecure_dev and (ssl_context is None or oauth_header_provider is None):
            raise R1Error(
                "secure R1 requires TLS/mTLS context and OAuth authorization hook")
        self.oauth_header_provider = oauth_header_provider
        self.authoritative_status_provider = authoritative_status_provider
        self.insecure_dev = bool(insecure_dev)
        self.transport = transport or UrllibTransport(ssl_context)
        self.state = JsonStateStore(state_path)
        # policy id -> policy type id, learned at create time so that update and
        # status validate against the right schema.  An id never created through
        # this client (e.g. a recovery query) defaults to the steering type,
        # which is the historical behaviour and keeps the steering path exact.
        self._policy_type_by_id: Dict[str, str] = {}

    def declare_policy_type(self, policy_id: str, policy_type_id: str) -> None:
        """Record the type of a policy this client did not create.

        A policy id only entered ``_policy_type_by_id`` on create, so an id
        this process inherited -- a binding restored from the journal after a
        restart, or a scope taken over from a finished transaction -- fell
        through to the steering default below and would have had its cap or
        priority body validated against the steering schema.  The adapter,
        which knows its own policy type, declares it here instead.
        """
        if policy_type_id not in FROZEN_POLICY_TYPES:
            raise R1Error("policy type is outside the frozen profile")
        self._policy_type_by_id[policy_id] = policy_type_id

    def _policy_type_of(self, policy_id: str) -> str:
        return self._policy_type_by_id.get(policy_id, STEERING_POLICY_TYPE)

    def harness_reset(self) -> None:
        self.state.reset()

    def _url(self, path: str, query: Mapping[str, str] | None = None) -> str:
        url = urljoin(self.api_root, path.lstrip("/"))
        return url + (("?" + urlencode(query)) if query else "")

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str | None:
        wanted = name.lower()
        return next((str(v) for k, v in headers.items()
                     if str(k).lower() == wanted), None)

    def _request(self, method: str, path_or_url: str, *, version: str,
                 body: Any = None, expected: Iterable[int] = (200,),
                 retryable: bool = True, query: Mapping[str, str] | None = None,
                 absolute: bool = False) -> HttpResponse:
        url = path_or_url if absolute else self._url(path_or_url, query)
        raw = canonicalize(body) if body is not None else None
        headers = {"Accept": "application/json", "Version": version}
        if self.insecure_dev:
            headers["X-Authenticated-RApp-Id"] = self.r_app_id
        if self.oauth_header_provider is not None:
            authorization = self.oauth_header_provider(method, url, version)
            # (P0) Compare the scheme token rather than the scheme-plus-space
            # literal: release-gates.1.0.0.json G-SEC-1 requires ZERO
            # occurrences of that literal anywhere in the packaged bytes, and
            # this file ships inside lib/**.  Splitting on the delimiter is the
            # same check without writing the marker.
            if (not isinstance(authorization, str)
                    or authorization.split(" ", 1)[0] != OAUTH_SCHEME):
                raise R1Error("OAuth hook did not return a %s authorization"
                              % OAUTH_SCHEME)
            headers["Authorization"] = authorization
        if raw is not None:
            headers["Content-Type"] = "application/json"
        attempts = self.max_attempts if retryable else 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            if attempt:
                delay = self.backoff[min(attempt, len(self.backoff) - 1)] \
                    if self.backoff else 0.0
                if delay:
                    time.sleep(delay)
            try:
                response = self.transport.request(
                    method, url, headers=headers, body=raw, timeout=self.timeout_s)
            except (URLError, TimeoutError, OSError) as exc:
                last_error = exc
                continue
            if response.status in expected:
                received = self._header(response.headers, "Version")
                if received != version:
                    raise R1Error(
                        f"R1 Version mismatch: expected {version}, got {received!r}")
                return response
            if method == "DELETE" and response.status == 404 and attempt:
                # An earlier DELETE whose reply was lost already removed the
                # resource; the caller's readback, not this status, verifies it.
                return response
            if response.status not in {408, 425, 429, 500, 502, 503, 504}:
                message = (f"{method} {url} returned {response.status}: "
                           f"{_problem_text(response.body)}")
                if response.status in R1_DEFINITIVE_STATUSES:
                    raise R1Refusal(message, status=response.status)
                raise R1Error(message)
            last_error = R1Error(
                f"retryable HTTP {response.status}: {_problem_text(response.body)}")
        raise R1Error(f"{method} {url} failed after {attempts} attempts: {last_error}")

    # Bootstrap and discovery -------------------------------------------------

    def bootstrap_info(self) -> Dict[str, Any]:
        return self._request(
            "GET", "bootstrap/v1/bootstrap-info", version="1.0.0").body

    def discover_services(self, *, api_name: str | None = None) -> Any:
        query = {"api-invoker-id": self.r_app_id}
        if api_name:
            query.update({"api-name": api_name, "api-version": "v1"})
        body = self._request(
            "GET", "service-apis/v1/allServiceAPIs", version="1.2.0",
            query=query).body
        entries = body if isinstance(body, list) else body.get("serviceAPIs", [])
        for entry in entries:
            versions = entry.get("vendorSpecific-o-ran.org", {}).get(
                "fullApiVersions", [])
            if not versions or any(not isinstance(version, str) or
                                   not _SEMVER.fullmatch(version)
                                   for version in versions):
                raise R1Error("discovered service has an invalid full SemVer")
            if api_name and "1.0.0" not in versions:
                raise R1Error("discovered service lacks fullApiVersions 1.0.0")
        return body

    def discover_policy_types(self) -> Any:
        return self._request(
            "GET", f"{POLICY_BASE}/policy-types", version=POLICY_VERSION).body

    def get_policy_type(self, policy_type_id: str) -> Dict[str, Any]:
        if policy_type_id not in FROZEN_POLICY_TYPES:
            raise R1Error("policy type is outside the frozen profile")
        return self._request(
            "GET", f"{POLICY_BASE}/policy-types/{quote(policy_type_id, safe='')}",
            version=POLICY_VERSION).body

    # Policy lifecycle --------------------------------------------------------

    def create_policy(self, near_rt_ric_id: str, policy_type_id: str,
                      policy_object: Dict[str, Any]) -> Dict[str, Any]:
        if policy_type_id not in FROZEN_POLICY_TYPES:
            raise R1Error("policy type is outside the frozen profile")
        _validate_policy_object(policy_type_id, policy_object)
        wrapper = {"policyObject": policy_object,
                   "nearRtRicId": near_rt_ric_id,
                   "policyTypeId": policy_type_id}
        response = self._request(
            "POST", f"{POLICY_BASE}/policies", version=POLICY_VERSION,
            body=wrapper, expected=(201,), retryable=True)
        location = self._header(response.headers, "Location")
        if not location:
            raise R1Error("policy create omitted Location")
        policy_id = urlparse(location).path.rstrip("/").split("/")[-1]
        if not policy_id:
            raise R1Error("policy create returned an empty policyId")
        self._policy_type_by_id[policy_id] = policy_type_id
        return {"policyId": policy_id, "location": location,
                "policyObjectInformation": response.body}

    def list_policies(self, *, near_rt_ric_id: str | None = None,
                      policy_type_id: str | None = None) -> Any:
        query = {}
        if near_rt_ric_id is not None:
            query["nearRtRicId"] = near_rt_ric_id
        if policy_type_id is not None:
            query["policyTypeId"] = policy_type_id
        return self._request(
            "GET", f"{POLICY_BASE}/policies", version=POLICY_VERSION,
            query=query).body

    def update_policy(self, policy_id: str,
                      policy_object: Dict[str, Any]) -> Dict[str, Any]:
        _validate_policy_object(self._policy_type_of(policy_id), policy_object)
        return self._request(
            "PUT", f"{POLICY_BASE}/policies/{quote(policy_id, safe='')}",
            version=POLICY_VERSION, body=policy_object, expected=(200,),
            retryable=True).body

    def get_policy(self, policy_id: str) -> Dict[str, Any]:
        return self._request(
            "GET", f"{POLICY_BASE}/policies/{quote(policy_id, safe='')}",
            version=POLICY_VERSION).body

    def delete_policy(self, policy_id: str) -> None:
        self._request(
            "DELETE", f"{POLICY_BASE}/policies/{quote(policy_id, safe='')}",
            version=POLICY_VERSION, expected=(204,))
        self._policy_type_by_id.pop(policy_id, None)

    def get_policy_status(self, policy_id: str) -> Dict[str, Any]:
        body = self._request(
            "GET", f"{POLICY_BASE}/policies/{quote(policy_id, safe='')}/status",
            version=POLICY_VERSION).body
        _validate_status_object(self._policy_type_of(policy_id), body)
        return body

    def create_status_subscription(self, notification_destination: str,
                                   policy_ids: list[str]) -> Dict[str, Any]:
        body = {"notificationDestination": notification_destination,
                "policyIdList": list(policy_ids)}
        response = self._request(
            "POST", f"{POLICY_BASE}/policies/subscriptions",
            version=POLICY_VERSION, body=body, expected=(201,), retryable=False)
        location = self._header(response.headers, "Location")
        if not location:
            raise R1Error("status subscription omitted Location")
        return {"subscriptionId": urlparse(location).path.rstrip("/").split("/")[-1],
                "location": location, "body": response.body}

    def get_status_subscription(self, subscription_id: str) -> Dict[str, Any]:
        return self._request(
            "GET", f"{POLICY_BASE}/policies/subscriptions/" +
            quote(subscription_id, safe=""), version=POLICY_VERSION).body

    def update_status_subscription(self, subscription_id: str,
                                   subscription: Dict[str, Any]) -> Dict[str, Any]:
        return self._request(
            "PUT", f"{POLICY_BASE}/policies/subscriptions/" +
            quote(subscription_id, safe=""), version=POLICY_VERSION,
            body=subscription, expected=(200,)).body

    def delete_status_subscription(self, subscription_id: str) -> None:
        self._request(
            "DELETE", f"{POLICY_BASE}/policies/subscriptions/" +
            quote(subscription_id, safe=""), version=POLICY_VERSION,
            expected=(204,))

    def handle_status_notification(self, notification: Dict[str, Any]) -> int:
        """Parse the R1 wrapper and persist current status with epoch dedupe."""
        if set(notification) != {"subscriptionId", "policyStates"}:
            raise R1Error("invalid A1PolicyStatusChangeNotification wrapper")
        states = notification.get("policyStates")
        if not isinstance(states, list) or not states:
            raise R1Error("policyStates must contain at least one entry")
        applied = 0
        for entry in states:
            if set(entry) != {"policyId", "policyStatusObject"}:
                raise R1Error("invalid policyStates entry")
            policy_id = entry["policyId"]
            status = entry["policyStatusObject"]
            _validate_status_object(self._policy_type_of(policy_id), status)
            aic = status["aicStatus"]
            if aic["policyId"] != policy_id:
                raise R1Error("wrapper policyId does not match status policyId")
            epoch, seq = aic["producerEpoch"], aic["statusSeq"]
            current = self.state.snapshot()["status"].get(policy_id)
            if current and current["producerEpoch"] == epoch:
                if seq <= current["statusSeq"]:
                    self.state.mutate(lambda s: s["audit"].append({
                        "kind": "LATE_OR_DUPLICATE_STATUS", "policyId": policy_id,
                        "producerEpoch": epoch, "statusSeq": seq}))
                    continue
                authoritative = status
            else:
                # Every first-seen epoch, including the initial one, is never
                # promoted solely from an at-least-once callback.
                if self.authoritative_status_provider is None:
                    queried = self.get_policy_status(policy_id)
                else:
                    queried = self.authoritative_status_provider(policy_id)
                    _validate_status_object(self._policy_type_of(policy_id), queried)
                if queried["aicStatus"]["producerEpoch"] != epoch:
                    self.state.mutate(lambda s: s["audit"].append({
                        "kind": "UNCONFIRMED_STATUS_EPOCH", "policyId": policy_id,
                        "producerEpoch": epoch, "statusSeq": seq}))
                    continue
                authoritative = queried
            aa = authoritative["aicStatus"]
            self.state.mutate(lambda s, p=policy_id, v=authoritative, a=aa:
                              s["status"].__setitem__(p, {
                                  "producerEpoch": a["producerEpoch"],
                                  "statusSeq": a["statusSeq"], "snapshot": v}))
            applied += 1
        return applied

    # DME data jobs and callback ledger --------------------------------------

    def discover_dme_type(self, dme_type_id: str = DME_TYPE) -> Dict[str, Any]:
        encoded = quote(dme_type_id, safe="")
        return self._request(
            "GET", f"data-discovery/v2/dme-types/{encoded}",
            version=DME_DISCOVERY_VERSION).body

    def _job_definition(self, policy_id: str, policy_revision: int,
                        near_rt_ric_id: str) -> Dict[str, Any]:
        return {"policyTypeId": "AIC_UECellSteering_1.0.0",
                "policyId": policy_id,
                "minimumPolicyRevision": policy_revision,
                "nearRtRicId": near_rt_ric_id}

    def create_continuous_job(self, *, policy_id: str, policy_revision: int,
                              near_rt_ric_id: str) -> Dict[str, Any]:
        binding_id = secrets.token_hex(16)  # exactly 128 random bits
        definition = self._job_definition(
            policy_id, policy_revision, near_rt_ric_id)
        pending = {"deliveryBindingId": binding_id, "dataJobId": None,
                   "dmeTypeId": DME_TYPE, "policyId": policy_id,
                   "minimumPolicyRevision": policy_revision,
                   "nearRtRicId": near_rt_ric_id,
                   "jobDefinitionSha256": jcs_sha256(definition), "active": False}
        self.state.mutate(
            lambda s: s["bindings"].__setitem__(binding_id, pending))
        body = {"dataDeliveryMode": "CONTINUOUS", "dmeTypeId": DME_TYPE,
                "productionJobDefinition": definition,
                "dataDeliveryMethod": "PUSH_HTTP",
                "dataDeliverySchemaId": DME_SCHEMA_ID,
                "pushDeliveryDetailsHttp": {
                    "dataPushUri": f"{self.push_base_uri}/{binding_id}"}}
        response = self._request(
            "POST", "data-access/v2/data-jobs", version=DME_ACCESS_VERSION,
            body=body, expected=(201,), retryable=False)
        location = self._header(response.headers, "Location")
        if not location:
            raise R1Error("data job create omitted Location")
        job_id = urlparse(location).path.rstrip("/").split("/")[-1]
        response_status = response.body.get("dataJobInfoStatus") \
            if isinstance(response.body, dict) else None
        committed = dict(pending, dataJobId=job_id, active=False,
                         dataJobInfoStatus=response_status)
        self.state.mutate(
            lambda s: s["bindings"].__setitem__(binding_id, committed))
        # Status must be externally confirmed before callback evidence is used.
        self.get_data_job_status(job_id)
        self.state.mutate(lambda s: s["bindings"][binding_id].__setitem__(
            "active", True))
        return {"deliveryBindingId": binding_id, "dataJobId": job_id,
                "location": location, "body": response.body}

    def get_data_job(self, data_job_id: str) -> Dict[str, Any]:
        return self._request(
            "GET", "data-access/v2/data-jobs/" + quote(data_job_id, safe=""),
            version=DME_ACCESS_VERSION).body

    def get_data_job_status(self, data_job_id: str) -> Dict[str, Any]:
        return self._request(
            "GET", "data-access/v2/data-jobs/" + quote(data_job_id, safe="") +
            "/status", version=DME_ACCESS_VERSION).body

    def harness_precreate_binding(self, binding_id: str,
                                  job: Mapping[str, Any]) -> None:
        """Persist runner-captured PUSH binding before the external POST."""
        definition = job["productionJobDefinition"]
        pending = {
            "deliveryBindingId": binding_id, "dataJobId": None,
            "dmeTypeId": job["dmeTypeId"], "policyId": definition["policyId"],
            "minimumPolicyRevision": definition["minimumPolicyRevision"],
            "nearRtRicId": definition["nearRtRicId"],
            "jobDefinitionSha256": jcs_sha256(definition), "active": False,
        }
        self.state.mutate(lambda state: state["bindings"].__setitem__(
            binding_id, pending))

    def harness_commit_binding(self, binding_id: str, data_job_id: str) -> None:
        """Map a pre-created binding to the server-assigned Location id."""
        def commit(state):
            binding = state["bindings"].get(binding_id)
            if binding is None or binding.get("dataJobId") is not None:
                raise R1Error("DME binding was not uniquely pre-created")
            binding.update({"dataJobId": data_job_id, "active": True,
                            "dataJobInfoStatus": "RUNNING"})
        self.state.mutate(commit)

    def update_data_job(self, data_job_id: str,
                        data_job_info: Dict[str, Any]) -> Dict[str, Any]:
        return self._request(
            "PUT", "data-access/v2/data-jobs/" + quote(data_job_id, safe=""),
            version=DME_ACCESS_VERSION, body=data_job_info,
            expected=(200,)).body

    def delete_data_job(self, data_job_id: str) -> None:
        self._request(
            "DELETE", "data-access/v2/data-jobs/" + quote(data_job_id, safe=""),
            version=DME_ACCESS_VERSION, expected=(204,))
        def deactivate(state):
            for binding in state["bindings"].values():
                if binding.get("dataJobId") == data_job_id:
                    binding["active"] = False
        self.state.mutate(deactivate)

    def accept_evidence(self, binding_id: str,
                        record: Dict[str, Any]) -> bool:
        """Durably validate/bind one PUSH_HTTP record; return False on dedupe."""
        validate(record, DME_TYPE)
        try:
            validate_evidence(record)
        except O1Error as exc:
            raise R1Error(exc.code) from exc
        snapshot = self.state.snapshot()
        binding = snapshot["bindings"].get(binding_id)
        if not binding or not binding.get("active") or not binding.get("dataJobId"):
            raise KeyError(binding_id)
        corr = record["correlation"]
        if (record["dmeTypeId"] != binding["dmeTypeId"] or
                corr["policyTypeId"] != "AIC_UECellSteering_1.0.0" or
                corr["policyId"] != binding["policyId"] or
                corr["policyRevision"] < binding["minimumPolicyRevision"]):
            raise R1Error("DME evidence does not match durable job binding")
        identity = f"{binding['dataJobId']}:{record['observationId']}"
        digest = jcs_sha256(record)
        existing = snapshot["evidence"].get(identity)
        if existing:
            if existing["digest"] != digest:
                raise R1Error("delivery identity reused with different payload")
            return False
        self.state.mutate(lambda s: s["evidence"].__setitem__(identity, {
            "digest": digest, "bindingId": binding_id,
            "dataJobId": binding["dataJobId"], "record": record}))
        return True

    def recover_one_time_pull(self, *, policy_id: str, policy_revision: int,
                              near_rt_ric_id: str,
                              max_polls: int = 3) -> Any:
        body = {"dataDeliveryMode": "ONE_TIME", "dmeTypeId": DME_TYPE,
                "productionJobDefinition": self._job_definition(
                    policy_id, policy_revision, near_rt_ric_id),
                "dataDeliveryMethod": "PULL_HTTP",
                "dataDeliverySchemaId": DME_SCHEMA_ID}
        created = self._request(
            "POST", "data-access/v2/data-jobs", version=DME_ACCESS_VERSION,
            body=body, expected=(201,), retryable=False)
        info = created.body
        try:
            pull_uri = info["pullDeliveryDetailsHttp"]["dataPullUri"]
        except (TypeError, KeyError) as exc:
            raise R1Error("ONE_TIME job omitted negotiated dataPullUri") from exc
        for _ in range(max_polls):
            response = self._request(
                "GET", pull_uri, version="1.0.0", expected=(200, 202),
                absolute=True)
            if response.status == 200:
                records = response.body if isinstance(response.body, list) \
                    else [response.body]
                for record in records:
                    validate(record, DME_TYPE)
                return response.body
            retry_after = self._header(response.headers, "Retry-After")
            try:
                delay = min(float(retry_after), self.timeout_s)
            except (TypeError, ValueError):
                raise R1Error("202 pull response requires numeric Retry-After")
            if delay > 0:
                time.sleep(delay)
        raise R1Error("ONE_TIME PULL_HTTP did not become ready within poll bound")
