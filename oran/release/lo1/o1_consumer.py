"""Composition boundary for live O1 notification, SFTP, and normalization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping
from urllib.parse import urlsplit

from .o1_netconf import OptionalNetconfAdapter
from .o1_notify import DurableNotificationJournal, NotificationRefused, parse_file_ready, raw_name
from .o1_pm import LiveInvariantError, LivePmNormalizer, measurement_values
from .o1_readiness import (
    PROVIDER_INITIAL_STATE,
    FrozenReadinessPostconditions,
    O1ReadinessLedger,
    ReadinessRefused,
)
from .o1_sftp import PinnedSftpClient, parse_sftp_location, validate_retrieval_window

if TYPE_CHECKING:
    from oran.release.lo1.capture import CaptureRecorder
    from oran.release.lo1.gate import GateResult
    from oran.release.lo1.tls import SecretResolver


TLS_PEER_AUTHORITY_HEADER = "x-lo1-internal-tls-peer-authority"  # legacy test seam only
SUBSCRIPTION_ID_HEADER = "x-lo1-internal-subscription-id"
TRUSTED_TLS_CERT_SHA256_HEADER = "x-lo1-trusted-tls-peer-certificate-sha256"
TRUSTED_TLS_ROLE_HEADER = "x-lo1-trusted-tls-peer-role"
TRUSTED_TCP_PEER_HEADER = "x-lo1-trusted-tcp-peer-authority"


@dataclass(frozen=True)
class O1ConsumerEndpoints:
    notification_recipient: str


@dataclass(frozen=True)
class RetrievedFile:
    location: str
    raw_sha256: str
    byte_count: int
    artifact_path: str
    file_info: Mapping[str, Any]


class NotificationAuthorizationRefused(NotificationRefused):
    """The HTTPS peer failed mTLS authentication or OAuth authorization."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = int(status)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def policy_cell_mappings(vector: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Select the serving/target policy cells from a possibly larger inventory."""
    topology = vector.get("topology", {})
    wanted = [topology.get("servingCell", {}).get("cellId"), topology.get("targetCell", {}).get("cellId")]
    wanted_keys = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in wanted]
    if any(item is None for item in wanted) or len(set(wanted_keys)) != len(wanted_keys):
        raise LiveInvariantError("servingCell and targetCell must identify two distinct policy cells")
    selected = [
        json.loads(json.dumps(item))
        for item in topology.get("cellMappings", [])
        if json.dumps(item.get("cellId"), sort_keys=True, separators=(",", ":")) in wanted_keys
    ]
    if len(selected) != len(wanted_keys):
        raise LiveInvariantError("policy cells do not resolve exactly once in cellMappings")
    return selected


def _raw_descriptor(raw_store: Any, raw: bytes, *, name: str, media_type: str) -> dict[str, Any]:
    result = dict(raw_store.put(raw, kind="o1", name=name, media_type=media_type))
    aliases = {
        "sha256": ("sha256", "rawSha256", "byteSha256"),
        "byteCount": ("byteCount", "bytes", "size"),
        "artifactPath": ("artifactPath", "path", "rawArtifactPath"),
    }
    normalized = {}
    for target, candidates in aliases.items():
        for candidate in candidates:
            if candidate in result:
                normalized[target] = result[candidate]
                break
    if set(normalized) != set(aliases):
        raise RuntimeError("RawStore.put did not return the frozen raw-byte descriptor")
    return normalized


def _mns_authority(vector: Mapping[str, Any]) -> str:
    parsed = urlsplit(str(vector["o1"]["fileDataReporting"]["mnsRoot"]))
    try:
        port = parsed.port
    except ValueError as exc:
        raise NotificationRefused("MnS root has an invalid authority") from exc
    if not parsed.hostname or port is None:
        raise NotificationRefused("MnS root must carry an explicit host and port")
    return f"[{parsed.hostname}]:{port}" if ":" in parsed.hostname else f"{parsed.hostname}:{port}"


class O1LiveConsumer:
    def __init__(
        self,
        *,
        gate: "GateResult",
        vector: Mapping[str, Any],
        recorder: "CaptureRecorder",
        resolver: "SecretResolver",
        profile_bytes: Mapping[str, bytes],
        security_authority: Mapping[str, Any] | None = None,
        oauth_validator: Any = None,
    ) -> None:
        gate.require_admitted()
        self.gate = gate
        self.vector = json.loads(json.dumps(vector))
        self.recorder = recorder
        self.resolver = resolver
        self.profile_bytes = dict(profile_bytes)
        self.security_authority = (json.loads(json.dumps(security_authority))
                                   if security_authority is not None else None)
        self._legacy_test_security = security_authority is None
        if security_authority is not None and oauth_validator is None:
            from .security import OAuthIntrospectionValidator
            oauth_validator = OAuthIntrospectionValidator(
                security_authority["oauth"]["notificationReceiver"], resolver,
                timeout=self._netconf_timeout_ms() / 1000.0)
        self.oauth_validator = oauth_validator
        self._mns_oauth = None
        if security_authority is not None:
            from .security import OAuthClientCredentials
            self._mns_oauth = OAuthClientCredentials(
                security_authority["oauth"]["mnsClient"], resolver,
                timeout=self._netconf_timeout_ms() / 1000.0)
        self.capture_root = Path(gate.capture_root)
        self.journal = DurableNotificationJournal(
            self.capture_root / "state/o1-notifications.json"
        )
        self._active = False
        self._cleaned = False
        self._cleanup_actions: list[dict[str, Any]] = []
        self._normalization_invocations = 0
        self._normalization_rejections = 0
        self._parser_invocations = 0
        self._sftp_backend = None
        self._retrieved_files: list[RetrievedFile] = []
        self._notifications_accepted = 0
        self._raw_store = getattr(recorder, "raw_store", None) or getattr(
            recorder, "_raw_store", None
        )
        if self._raw_store is None:
            try:
                from oran.release.lo1.capture import RawStore

                self._raw_store = RawStore(capture_root=self.capture_root)
            except (ImportError, TypeError) as exc:
                raise RuntimeError("the frozen RawStore capture seam is unavailable") from exc
        assignment_digest = None
        if hasattr(gate, "as_capture_authority"):
            authority = gate.as_capture_authority()
            assignment_digest = authority.get("authorityRecordDigest")
        # The §10.2 ledger.  Built from the frozen runner contract's own
        # postcondition strings, so nothing in this release restates them.
        self.readiness = O1ReadinessLedger(
            postconditions=FrozenReadinessPostconditions(self.profile_bytes),
            clock=getattr(recorder, "clock", None))
        accepted = list(
            (getattr(gate, "observations", None) or {}).get(
                "providerEstablishedInitialStates", []))
        self.readiness.stage(
            "ASSURANCE_READY",
            providerAcceptedInitialState=PROVIDER_INITIAL_STATE in accepted,
            providerAcceptanceSource="provider-acceptance-contract"
                                     "#/acceptance/providerEstablishedInitialStates")
        self.netconf = OptionalNetconfAdapter(
            assigned_actions=frozenset(gate.netconf_adapter_actions),
            recorder=recorder,
            authority_record_digest=assignment_digest,
            vector=self.vector,
            resolver=resolver,
            profile_bytes=self.profile_bytes,
            guard=self._egress_guard(),
            allowed_authorities=tuple(gate.allowlist),
            capture_root=self.capture_root,
            ssl_context=self._provider_ssl_context(),
            timeout_ms=self._netconf_timeout_ms(),
            readiness=self.readiness,
            drain=self._drain_in_flight,
            oauth_header_provider=(self._mns_oauth.authorization_header
                                   if self._mns_oauth is not None else None),
        )
        recorder.set_o1_readiness(self.readiness.as_capture())
        sftp = self.vector.get("o1", {}).get("sftp", {})
        netconf = self.vector.get("o1", {}).get("netconf", {})
        for reference in (sftp.get("knownHostsRef"), sftp.get("credentialRef"),
                          netconf.get("knownHostsRef") if self.netconf.enabled else None,
                          netconf.get("credentialRef") if self.netconf.enabled else None):
            if isinstance(reference, str):
                recorder.note_secret_reference(reference)

    # -- composition seams -------------------------------------------------
    def _egress_guard(self) -> Any:
        return getattr(self.recorder, "egress_guard", None) or getattr(
            self.recorder, "_egress", None)

    def _netconf_timeout_ms(self) -> int:
        timeouts = self.vector.get("timeouts", {})
        try:
            return int(timeouts.get("defaultStepMs") or 30000)
        except (TypeError, ValueError):
            return 30000

    def _provider_ssl_context(self) -> Any:
        """The Provider trust anchor for the FileDataReporting hop, or nothing.

        An unresolvable truststore is not downgraded here: the subscription's
        own request would then run without a verified anchor, so the resolver is
        asked and a miss leaves the context absent, which fails closed at the
        first HTTPS call instead of connecting unverified.
        """
        if self.security_authority is not None:
            tls_profile = self.security_authority["tls"]["mnsClient"]
            reference = tls_profile["truststoreRef"]
        else:
            tls_profile = None
            reference = self.vector.get("security", {}).get("truststoreRef")
        if not isinstance(reference, str):
            return None
        has = getattr(self.resolver, "has", None)
        if callable(has) and not has(reference):
            return None
        try:
            from .tls import build_client_ssl_context

            return build_client_ssl_context(
                truststore_ref=reference, resolver=self.resolver,
                client_certificate_ref=(tls_profile or {}).get(
                    "clientCertificateRef"),
                client_private_key_ref=(tls_profile or {}).get(
                    "clientPrivateKeyRef"))
        except Exception:  # noqa: BLE001 - absence is fail-closed, not a downgrade
            return None

    @property
    def endpoints(self) -> O1ConsumerEndpoints:
        return O1ConsumerEndpoints(
            notification_recipient=str(
                self.vector["o1"]["fileDataReporting"]["consumerReference"]
            )
        )

    def start(self) -> None:
        if self._active:
            return
        if self._cleaned:
            raise RuntimeError("cleaned O1 consumer cannot restart; create a fresh run")
        if self.journal.path.exists():
            raise RuntimeError("durable notification state already exists")
        try:
            self.netconf.ensure_startable()
        except Exception:
            self.cleanup(failure=True)
            raise
        # §10.2 item 2: the notification receiver is the upper's half of
        # TRUST_READY.  It is staged, not confirmed -- the NETCONF session's half
        # is measured by the lifecycle, and neither component asserts the other's.
        self.readiness.stage("TRUST_READY", notificationReceiverBound=True)
        self.recorder.set_o1_readiness(self.readiness.as_capture())
        self._active = True

    def _require_active(self) -> None:
        if not self._active:
            raise RuntimeError("O1 live consumer is not started")

    def _require_ready(self, what: str) -> None:
        """The measured SC-084 body may not start before readiness is established.

        1.0.1 let a notification, a retrieval and a normalization run -- and a
        capture reach ``COMPLETED`` -- with no subscription and no PerfMetricJob
        in existence.  Each measured entry point now asks the ledger first, and
        the refusal carries the unmet state's name.
        """
        self._require_active()
        self.readiness.require_measured_body(what)

    def _drain_in_flight(self) -> dict[str, Any]:
        """What §10.2 item 9 drains between the job delete and the unsubscribe."""
        return {"inFlight": self.readiness.in_flight}

    def accept_notification(
        self, *, headers: Mapping[str, str], raw: bytes, peer: str
    ) -> tuple[int, dict[str, Any]]:
        self._require_active()
        descriptor = None
        sequence = -1
        received_at = ""
        peer_authenticated = False
        subscription_matched = False
        try:
            with self.readiness.measured_body(
                    "accepting a notifyFileReady notification"):
                sequence = self.recorder.next_sequence()
                received_at = _now()
                descriptor = _raw_descriptor(
                    self._raw_store,
                    raw,
                    name=raw_name(raw),
                    media_type="application/json",
                )
                normalized_headers = {
                    str(key).lower(): str(value) for key, value in headers.items()
                }
                expected_subscription = str(
                    self.readiness.confirmed_evidence("SUBSCRIBED")
                    ["subscriptionId"])
                if self._legacy_test_security:
                    peer_authenticated = (
                        normalized_headers.get(TLS_PEER_AUTHORITY_HEADER) == peer
                        and peer == _mns_authority(self.vector)
                        and peer in set(self.gate.allowlist))
                else:
                    tls_profile = self.security_authority["tls"][
                        "notificationReceiver"]
                    roles = {str(item["certificateSha256"]): str(item["role"])
                             for item in tls_profile["allowedClientIdentities"]}
                    certificate = normalized_headers.get(
                        TRUSTED_TLS_CERT_SHA256_HEADER)
                    role = normalized_headers.get(TRUSTED_TLS_ROLE_HEADER)
                    peer_authenticated = bool(
                        certificate and roles.get(certificate) ==
                        "LOWER_LIVE_O1_PROVIDER" and
                        role == "LOWER_LIVE_O1_PROVIDER")
                    if not peer_authenticated:
                        raise NotificationAuthorizationRefused(
                            "mutual TLS did not authenticate an authorized Provider certificate",
                            status=401)
                    if self.oauth_validator is None:
                        raise NotificationAuthorizationRefused(
                            "OAUTH_VALIDATION_AUTHORITY_UNSPECIFIED", status=401)
                    try:
                        self.oauth_validator.validate(
                            normalized_headers.get("authorization"))
                    except Exception as exc:
                        from .security import OAuthAuthorizationError
                        raise NotificationAuthorizationRefused(
                            "OAuth notification authorization failed: %s" % exc,
                            status=(403 if isinstance(
                                exc, OAuthAuthorizationError) else 401)) from exc
                    peer = normalized_headers.get(
                        TRUSTED_TCP_PEER_HEADER, "unattributed-peer:0")
                subscription_matched = (
                    normalized_headers.get(SUBSCRIPTION_ID_HEADER)
                    == expected_subscription
                )
                if not peer_authenticated:
                    raise NotificationRefused(
                        "TLS boundary did not attest the gate-bound MnS peer authority"
                    )
                if not subscription_matched:
                    raise NotificationRefused(
                        "TLS boundary did not bind the active subscription identifier"
                    )
                expected_job = str(self.vector["o1"]["perfMetricJob"]["jobId"])
                document = parse_file_ready(raw, expected_job_id=expected_job)
                duplicate = self.journal.append(
                    document,
                    raw_sha256=str(descriptor["sha256"]),
                    raw_artifact_path=str(descriptor["artifactPath"]),
                )
                record = {
                    "sequence": sequence,
                    "receivedAt": received_at,
                    "peerAuthority": peer,
                    "peerAuthenticated": peer_authenticated,
                    "raw": descriptor,
                    "responseStatus": 204,
                    "durablyAcceptedBeforeResponse": True,
                    "accepted": True,
                    "subscriptionIdMatched": subscription_matched,
                    "eventTime": document["eventTime"],
                    "href": document["href"],
                    "fileInfoList": [
                        {
                            key: info[key]
                            for key in (
                                "fileLocation", "fileReadyTime",
                                "fileExpirationTime", "fileSize",
                                "fileDataType", "jobId",
                            )
                            if key in info
                        }
                        for info in document["fileInfoList"]
                    ],
                }
                self.recorder.emit_notification(record)
                self._notifications_accepted += 1
                return 204, {
                    "durablyAccepted": True,
                    "duplicate": duplicate,
                    "fileInfoList": document["fileInfoList"],
                }
        except ReadinessRefused:
            # Admission refusal has zero side effects: it is not a failed run
            # that needs cleanup, because the measured operation never began.
            raise
        except Exception as exc:
            failure_status = int(getattr(exc, "status", 400))
            try:
                if descriptor is not None:
                    self.recorder.emit_notification(
                        {
                            "sequence": sequence,
                            "receivedAt": received_at,
                            "peerAuthority": peer if ":" in peer else "invalid:1",
                            "peerAuthenticated": peer_authenticated,
                            "raw": descriptor,
                            "responseStatus": failure_status,
                            "durablyAcceptedBeforeResponse": False,
                            "accepted": False,
                            "rejectReason": str(exc),
                            "subscriptionIdMatched": (
                                subscription_matched and peer_authenticated
                            ),
                            "href": "(rejected)",
                            "fileInfoList": [],
                        }
                    )
            finally:
                self.cleanup(failure=True)
            raise

    def retrieve(self, *, file_info: Mapping[str, Any]) -> RetrievedFile:
        self._require_active()
        sequence = -1
        started = ""
        sftp_config = self.vector["o1"]["sftp"]
        known_ref = str(sftp_config["knownHostsRef"])
        credential_ref = str(sftp_config["credentialRef"])
        guard = getattr(self.recorder, "egress_guard", None) or getattr(
            self.recorder, "_egress", None
        )
        target = None
        try:
            with self.readiness.measured_body("retrieving a PM file over SFTP"):
                sequence = self.recorder.next_sequence()
                started = _now()
                target = parse_sftp_location(str(file_info.get("fileLocation", "")))
                validate_retrieval_window(file_info, retrieved_at=started)
                client = PinnedSftpClient(
                    allowed_authorities=tuple(sftp_config["allowedAuthorities"]),
                    known_hosts_path=self.resolver.resolve_path(known_ref),
                    credential_path=self.resolver.resolve_path(credential_ref),
                    guard=guard,
                    backend=self._sftp_backend,
                )
                fetched = client.fetch(
                    str(file_info["fileLocation"]),
                    expected_size=int(file_info["fileSize"])
                )
                completed = _now()
                descriptor = _raw_descriptor(
                    self._raw_store,
                    fetched.raw,
                    name="pm-%s.xml" % hashlib.sha256(fetched.raw).hexdigest(),
                    media_type="application/xml",
                )
                enriched = dict(file_info)
                enriched.update(
                    {
                        "_retrievalSequence": sequence,
                        "_retrievedAt": completed,
                        "_hostKeyPinDigest": fetched.host_key_pin_digest,
                    }
                )
                self.recorder.emit_retrieval(
                    {
                        "sequence": sequence,
                        "sourceStepId": "o1-notify",
                        "sourceOfLocation": "CAPTURED_FILE_LOCATION",
                        "requestedLocation": str(file_info["fileLocation"]),
                        "requestedAuthority": target.authority,
                        "authorityAllowed": True,
                        "hostKeyPinDigest": fetched.host_key_pin_digest,
                        "hostKeyVerified": True,
                        "credentialRef": credential_ref,
                        "startedAt": started,
                        "completedAt": completed,
                        "outcome": "RETRIEVED",
                        "byteCount": descriptor["byteCount"],
                        "byteSha256": descriptor["sha256"],
                        "rawArtifactPath": descriptor["artifactPath"],
                        "attemptCount": 1,
                    }
                )
                retrieved = RetrievedFile(
                    location=str(file_info["fileLocation"]),
                    raw_sha256=str(descriptor["sha256"]),
                    byte_count=int(descriptor["byteCount"]),
                    artifact_path=str(descriptor["artifactPath"]),
                    file_info=enriched,
                )
                self._retrieved_files.append(retrieved)
                return retrieved
        except ReadinessRefused:
            raise
        except Exception:
            try:
                completed = _now()
                empty = _raw_descriptor(
                    self._raw_store,
                    b"",
                    name="retrieval-%s-error.bin" % sequence,
                    media_type="application/octet-stream",
                )
                self.recorder.emit_retrieval(
                    {
                        "sequence": sequence,
                        "sourceStepId": "o1-notify",
                        "sourceOfLocation": "CAPTURED_FILE_LOCATION",
                        "requestedLocation": str(file_info.get("fileLocation", "(absent)")),
                        "requestedAuthority": target.authority if target is not None else "invalid:1",
                        "authorityAllowed": bool(
                            target is not None
                            and target.authority in set(sftp_config["allowedAuthorities"])
                        ),
                        "hostKeyPinDigest": hashlib.sha256(b"").hexdigest(),
                        "hostKeyVerified": False,
                        "credentialRef": credential_ref,
                        "startedAt": started,
                        "completedAt": completed,
                        "outcome": "ERROR",
                        "byteCount": 0,
                        "byteSha256": empty["sha256"],
                        "rawArtifactPath": empty["artifactPath"],
                        "attemptCount": 1,
                    }
                )
            except Exception:
                # The primary retrieval failure remains authoritative even when
                # its best-effort failure artifact cannot be persisted.
                pass
            finally:
                self.cleanup(failure=True)
            raise

    def _profiles(self) -> tuple[dict[str, set[int | float]], int]:
        golden: dict[str, set[int | float]] = {}
        profile = None
        for name, raw in self.profile_bytes.items():
            normalized = name.replace("\\", "/")
            if "golden/o1/" in normalized and normalized.endswith(".xml") and "/netconf/" not in normalized:
                for metric, values in measurement_values(raw).items():
                    golden.setdefault(metric, set()).update(values)
            if normalized.endswith("oran-aic-o1-pa-file.1.0.0.json"):
                profile = json.loads(raw)
        if profile is None or not golden:
            raise LiveInvariantError("frozen O1 profile and golden PM values are required")
        return golden, int(profile["time"]["evidenceFreshnessLimitMs"])

    def normalize(
        self, *, retrieved: RetrievedFile, context: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        self._require_active()
        try:
            with self.readiness.measured_body("normalizing a retrieved PM file"):
                self._normalization_invocations += 1
                artifact = (self.capture_root / retrieved.artifact_path).resolve()
                root = self.capture_root.resolve()
                if root not in artifact.parents or not artifact.is_file():
                    raise LiveInvariantError(
                        "retrieved raw artifact path escapes or is absent")
                raw = artifact.read_bytes()
                if (len(raw) != retrieved.byte_count
                        or hashlib.sha256(raw).hexdigest()
                        != retrieved.raw_sha256):
                    raise LiveInvariantError(
                        "retrieved raw artifact digest changed before parsing")
                declared_o1_rules = self._declared_o1_rules()
                golden, freshness = self._profiles()
                normalizer = LivePmNormalizer(
                    cell_mappings=policy_cell_mappings(self.vector),
                    expected_cell_count=int(
                        self.vector["o1"]["live"]["expectedPolicyCellCount"]),
                    golden_values=golden,
                    freshness_limit_ms=freshness,
                )
                self._parser_invocations += 1
                records, summary = normalizer.normalize(
                    raw=raw,
                    raw_sha256=retrieved.raw_sha256,
                    raw_artifact_path=retrieved.artifact_path,
                    retrieval_sequence=int(
                        retrieved.file_info["_retrievalSequence"]),
                    file_info=retrieved.file_info,
                    retrieved_at=str(retrieved.file_info["_retrievedAt"]),
                    evaluation_now=str(context.get("evaluationNow") or _now()),
                    correlation=context.get("correlation", {}),
                    policy_scope=context.get("policyScope", {}),
                )
                summary["invocations"] = self._normalization_invocations
                summary["parserInvocations"] = self._parser_invocations
                summary["rejectedCount"] = self._normalization_rejections
                self.recorder.set_normalization(summary)
                for rule_id in declared_o1_rules:
                    if hasattr(self.recorder, "add_applied_rule"):
                        self.recorder.add_applied_rule(
                            rule_id,
                            inputs=[retrieved.artifact_path],
                            observations={
                                "sourceFileSha256": retrieved.raw_sha256},
                        )
                self._confirm_assurance_ready(summary)
                return records
        except ReadinessRefused:
            raise
        except Exception:
            self._normalization_rejections += 1
            self.recorder.set_normalization(
                {
                    "invocations": self._normalization_invocations,
                    "parserInvocations": self._parser_invocations,
                    "commitEligibleCount": 0,
                    "rejectedCount": self._normalization_rejections,
                    "duplicateCount": 0,
                    "records": [],
                }
            )
            self.cleanup(failure=True)
            raise

    def _confirm_assurance_ready(self, summary: Mapping[str, Any]) -> None:
        """§10.2 item 5, once the first full cycle has actually succeeded.

        The state is defined by the cycle, not claimed before it: notification
        accepted durably, file retrieved and verified, normalization produced a
        commit-eligible record.  ``providerAcceptedInitialState`` was staged from
        the Provider acceptance contract at construction, so a run whose Provider
        never accepted ``CONFIGURE_LIVE_O1_OK_RECORD_FOR_EACH_POLICY_CELL``
        cannot reach it however well the upper's own half went.
        """
        if self.readiness.confirmed("ASSURANCE_READY"):
            return
        commit_eligible = int(summary.get("commitEligibleCount", 0))
        if commit_eligible < 1 or not self._retrieved_files \
                or self._notifications_accepted < 1:
            return
        try:
            self.readiness.confirm("ASSURANCE_READY", evidence={
                "notificationsAccepted": self._notifications_accepted,
                "filesRetrieved": len(self._retrieved_files),
                "commitEligibleRecords": commit_eligible,
            })
        except ReadinessRefused:
            # The upper's half succeeded and its evidence stands; the state does
            # not.  Leaving it unconfirmed is the fail-closed outcome -- the run
            # ends ABORTED_ERROR naming ASSURANCE_READY -- and it does not
            # rewrite a normalization that really did happen as a rejection.
            pass
        self.recorder.set_o1_readiness(self.readiness.as_capture())

    def _declared_o1_rules(self) -> tuple[str, ...]:
        for name, raw in self.profile_bytes.items():
            if name.replace("\\", "/").endswith("scenario-catalog.1.0.1.json"):
                document = json.loads(raw)
                scenario = document["scenarios"][83]
                if scenario.get("id") != "SC-084":
                    raise LiveInvariantError("frozen SC-084 catalog pointer changed")
                rules = scenario.get("rules")
                if (
                    not isinstance(rules, list)
                    or not all(isinstance(rule_id, str) for rule_id in rules)
                    or len(rules) != len(set(rules))
                ):
                    raise LiveInvariantError("frozen SC-084 catalog rules are malformed")
                if "RULE-O1-POSITIONAL-MAP" in rules:
                    raise LiveInvariantError("forbidden positional O1 rule is declared")
                o1_rules = tuple(
                    rule_id for rule_id in rules if rule_id.startswith("RULE-O1-")
                )
                if not o1_rules:
                    raise LiveInvariantError("frozen SC-084 catalog declares no O1 rules")
                return o1_rules
        raise LiveInvariantError("frozen SC-084 scenario catalog is required")

    def perf_metric_job(self, *, action: str) -> dict[str, Any]:
        try:
            return self.netconf.execute(action)
        except Exception:
            self.cleanup(failure=True)
            raise

    # Capture 2.0 reads these observation-only hooks.  The consumer neither
    # serializes nor interprets them; the NETCONF transport/lifecycle remains
    # the authority for exact octets and segment boundaries.
    def netconf_session_binding(self) -> dict[str, Any]:
        return dict(self.netconf.session_binding())

    def netconf_wire_evidence(self) -> dict[str, Any]:
        return dict(self.netconf.wire_evidence())

    def mark_measured_segment_end(self) -> dict[str, Any]:
        return dict(self.netconf.mark_measured_segment_end())

    def measured_segment_counters(self) -> dict[str, Any]:
        return dict(self.netconf.measured_segment_counters())

    def _cleanup_action(self, target: str, action: str, outcome: str, detail: str) -> dict[str, Any]:
        record = {
            "target": target,
            "action": action,
            "outcome": outcome,
            "at": _now(),
            "detail": detail,
        }
        self.recorder.emit_cleanup_action(record)
        self._cleanup_actions.append(record)
        return record

    def cleanup(self, *, failure: bool) -> list[dict[str, Any]]:
        if self._cleaned:
            return [dict(item) for item in self._cleanup_actions]
        # Admission closes before the first destructive transition.  Existing
        # leases remain visible to the drain; new measured work is refused.
        self.readiness.begin_draining()
        residual: list[str] = []
        if self.netconf.enabled:
            # The cleanup SEGMENT of the assigned lifecycle: the frozen teardown
            # ordinals, the subscription delete and its absence readback.  Its
            # actions are recorded by the adapter itself, so they are never
            # duplicated here and never asserted when nothing was started.
            try:
                teardown = self.netconf.teardown()
            except Exception as exc:  # noqa: BLE001 - observable cleanup failure
                teardown = [self._cleanup_action(
                    "NETCONF_LIFECYCLE", "TEARDOWN", "FAILED",
                    str(exc)[:4096])]
                residual.append("NETCONF_LIFECYCLE")
            if teardown:
                # Real lifecycle actions have already been emitted by the
                # adapter.  Locally generated exception evidence was appended by
                # ``_cleanup_action`` above and must not be duplicated.
                for item in teardown:
                    record = dict(item)
                    if record not in self._cleanup_actions:
                        self._cleanup_actions.append(record)
            else:
                self._cleanup_action(
                    "O1_SUBSCRIPTION",
                    "DELETE",
                    "ALREADY_ABSENT",
                    "assigned adapter failed closed before creating a subscription",
                )
                self._cleanup_action(
                    "PERF_METRIC_JOB",
                    "DELETE",
                    "ALREADY_ABSENT",
                    "assigned adapter failed closed before creating a job",
                )
            residual.extend(str(item) for item in self.netconf.residual())
            residual.extend(
                str(item.get("target", "CLEANUP"))
                for item in teardown if item.get("outcome") == "FAILED")
        else:
            self._cleanup_action(
                "O1_SUBSCRIPTION",
                "SKIPPED_NOT_OWNED",
                "SKIPPED",
                "optional adapter action is not assigned to the upper",
            )
            self._cleanup_action(
                "PERF_METRIC_JOB",
                "SKIPPED_NOT_OWNED",
                "SKIPPED",
                "optional adapter action is not assigned to the upper",
            )
        residual = list(dict.fromkeys(item for item in residual if item))
        if residual:
            # Do not erase the journal, close the listener, or mark the consumer
            # clean while a Provider-side resource or in-flight operation is
            # still observable.  A later operator-authorized retry receives the
            # durable identifiers it needs to finish the teardown.
            self.recorder.set_cleanup_path(
                path="FAILURE", complete=False, residual=residual)
            self.recorder.set_o1_readiness(self.readiness.as_capture())
            raise RuntimeError(
                "O1 cleanup stopped with residual: %s" % ", ".join(residual))
        self._cleanup_action(
            "SFTP_SESSION", "CLOSE", "ALREADY_ABSENT", "retrieval sessions are request-scoped"
        )
        try:
            removed = self.journal.clear()
            self._cleanup_action(
                "TEMP_STATE", "REMOVE", "DONE" if removed else "ALREADY_ABSENT", "notification journal"
            )
        except OSError as exc:
            residual.append("notification journal")
            self._cleanup_action("TEMP_STATE", "REMOVE", "FAILED", type(exc).__name__)
        if residual:
            self.recorder.set_cleanup_path(
                path="FAILURE", complete=False, residual=residual)
            self.recorder.set_o1_readiness(self.readiness.as_capture())
            raise RuntimeError(
                "O1 cleanup stopped with residual: %s" % ", ".join(residual))
        self._cleanup_action(
            "LISTENER", "CLOSE", "DONE" if self._active else "ALREADY_ABSENT", "O1 consumer listener"
        )
        self._active = False
        self._cleaned = True
        self.readiness.mark_stopped()
        self.recorder.set_cleanup_path(
            path="FAILURE" if failure else "NORMAL",
            complete=not residual,
            residual=residual,
        )
        # The teardown order the adapter observed is part of the readiness
        # evidence on BOTH paths; a failure teardown that skipped the drain is
        # visible here rather than only in a log.
        self.recorder.set_o1_readiness(self.readiness.as_capture())
        return [dict(item) for item in self._cleanup_actions]

    def stop(self) -> None:
        if not self._cleaned:
            self.cleanup(failure=False)
