"""Deterministic A1 policy to E2SM-RC worker with rollback and effect gating."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol

from oran.contract.jcs import jcs_sha256
from .a1 import A1Conflict, A1PolicyProducer, POLICY_TYPE_ID
from .codec import decode_control_request, encode_control_request
from .measurement import MockMeasurementCollector
from .model import PrbRatios, SliceIdentity, SliceQuota, quota_from_policy


class WorkerError(ValueError):
    pass


class ScopeConflict(WorkerError):
    pass


class FencingError(WorkerError):
    pass


class RollbackError(WorkerError):
    pass


class AmbiguousDeliveryError(WorkerError):
    pass


@dataclass(frozen=True)
class DeliveryReceipt:
    acknowledged: bool
    detail: str


@dataclass(frozen=True)
class ActuationOutcome:
    policy_type_id: str
    policy_id: str
    policy_digest: str
    delivery_acknowledged: bool
    readback_verified: bool
    measurement_correlated: bool
    core_evidence_correlated: bool
    enforced: bool
    previous_ratios: PrbRatios | None
    reason: str


class RcTransport(Protocol):
    def send(self, request: Mapping[str, Any], *, scope_key: str,
             fencing_token: int, idempotency_key: str) -> DeliveryReceipt: ...

    def readback(self, scope_key: str) -> PrbRatios | None: ...


class UeAnchorResolver(Protocol):
    def resolve(self, identity: SliceIdentity) -> str | None: ...


class MockUeAnchorResolver:
    """Hardware-free stand-in for the live UE-ID resolution adapter."""

    def __init__(self, anchors: Mapping[str, str]) -> None:
        self._anchors = dict(anchors)

    def resolve(self, identity: SliceIdentity) -> str | None:
        return self._anchors.get(identity.key())


@dataclass(frozen=True)
class _Attempt:
    policy_id: str
    policy_digest: str
    quota: SliceQuota
    trace_id: str
    ue_anchor_ref: str
    sent_at: datetime
    receipt: DeliveryReceipt
    previous: PrbRatios | None


class MockRcTransport:
    """Executable stand-in for FlexRIC/E2/OAI that provides typed readback."""

    def __init__(self, *, acknowledge: bool = True, apply_on_ack: bool = True) -> None:
        self.acknowledge = acknowledge
        self.apply_on_ack = apply_on_ack
        self.requests: list[dict[str, Any]] = []
        self._state: dict[str, PrbRatios] = {}
        self._last_token: dict[str, int] = {}

    def send(self, request: Mapping[str, Any], *, scope_key: str,
             fencing_token: int, idempotency_key: str) -> DeliveryReceipt:
        del idempotency_key
        if fencing_token <= self._last_token.get(scope_key, 0):
            raise FencingError("transport rejected stale fencing token")
        quotas = decode_control_request(request)
        if len(quotas) != 1 or quotas[0].identity.key() != scope_key:
            raise WorkerError("transport request scope differs from worker scope")
        self.requests.append(dict(request))
        self._last_token[scope_key] = fencing_token
        if self.acknowledge and self.apply_on_ack:
            self._state[scope_key] = quotas[0].ratios
        return DeliveryReceipt(self.acknowledge, "RIC Control Acknowledge" if self.acknowledge else "RIC Control Failure")

    def readback(self, scope_key: str) -> PrbRatios | None:
        return self._state.get(scope_key)

    def seed(self, scope_key: str, ratios: PrbRatios) -> None:
        self._state[scope_key] = ratios


class SliceActuationWorker:
    """One-policy-per-slice, fenced, idempotent Style 2/Action 6 worker."""

    def __init__(self, transport: RcTransport, collector: MockMeasurementCollector,
                 *, anchor_resolver: UeAnchorResolver,
                 producer: A1PolicyProducer | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._transport = transport
        self._collector = collector
        self._anchor_resolver = anchor_resolver
        self._producer = producer
        self._delete_capability = (
            producer.bind_delete_handler(self._delete_from_a1)
            if producer is not None else None
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._scope_owner: dict[str, str] = {}
        self._last_token: dict[str, int] = {}
        self._results: dict[tuple[str, str], ActuationOutcome] = {}
        self._attempts: dict[tuple[str, str], _Attempt] = {}
        self._rollback: dict[str, list[tuple[str, SliceIdentity, PrbRatios, str]]] = {}

    @staticmethod
    def scope_key(policy: Mapping[str, Any]) -> str:
        return quota_from_policy(policy).identity.key()

    def apply(self, policy_id: str, policy: Mapping[str, Any]) -> ActuationOutcome:
        quota = quota_from_policy(policy)
        try:
            trace = policy["trace"]
            trace_id = trace["traceId"]
            token = trace["fencingToken"]
            revision = trace["revision"]
        except (KeyError, TypeError) as exc:
            raise WorkerError("policy trace requires traceId, revision, and fencingToken") from exc
        if not isinstance(trace_id, str) or not trace_id:
            raise WorkerError("traceId must be a non-empty string")
        if isinstance(token, bool) or not isinstance(token, int) or token < 1:
            raise WorkerError("fencingToken must be a positive integer")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise WorkerError("revision must be a positive integer")
        try:
            not_before = datetime.fromisoformat(
                policy["validity"]["notBefore"].replace("Z", "+00:00"),
            )
            not_after = datetime.fromisoformat(
                policy["validity"]["notAfter"].replace("Z", "+00:00"),
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise WorkerError("policy validity requires RFC3339 notBefore and notAfter") from exc
        now = self._clock()
        if now.tzinfo is None or not_before.tzinfo is None or not_after.tzinfo is None:
            raise WorkerError("policy validity and worker clock must carry a timezone")
        if not not_before <= now < not_after:
            raise WorkerError("policy is outside its validity window")
        digest = jcs_sha256(policy)
        scope_key = quota.identity.key()
        owner = self._scope_owner.get(scope_key)
        if owner is not None and owner != policy_id:
            raise ScopeConflict(f"slice scope is already controlled by policy {owner}")
        result_key = (policy_id, digest)
        attempt = self._attempts.get(result_key)
        if attempt is not None:
            return self._evaluate(attempt, now)
        if token <= self._last_token.get(scope_key, 0):
            raise FencingError("stale fencing token")

        ue_anchor_ref = self._anchor_resolver.resolve(quota.identity)
        if not isinstance(ue_anchor_ref, str) or not ue_anchor_ref:
            raise WorkerError("Control Header Format 1 UE anchor is unresolved for this slice")

        previous = self._transport.readback(scope_key)
        if previous is None:
            raise RollbackError(
                "slice quota write requires a restorable previous readback",
            )
        request = encode_control_request((quota,), ue_anchor_ref=ue_anchor_ref)
        self._scope_owner[scope_key] = policy_id
        self._last_token[scope_key] = token
        history = self._rollback.setdefault(policy_id, [])
        if previous != quota.ratios and not history:
            history.append(
                (scope_key, quota.identity, previous, ue_anchor_ref),
            )
        try:
            receipt = self._transport.send(
                request, scope_key=scope_key, fencing_token=token,
                idempotency_key=f"{policy_id}:{revision}:{digest}",
            )
        except Exception as exc:
            attempt = _Attempt(
                policy_id=policy_id,
                policy_digest=digest,
                quota=quota,
                trace_id=trace_id,
                ue_anchor_ref=ue_anchor_ref,
                sent_at=now,
                receipt=DeliveryReceipt(False, f"ambiguous delivery: {exc}"),
                previous=previous,
            )
            self._attempts[result_key] = attempt
            self._evaluate(attempt, now)
            raise AmbiguousDeliveryError(
                "control delivery is ambiguous; retry is suppressed and rollback remains available",
            ) from exc
        attempt = _Attempt(
            policy_id=policy_id,
            policy_digest=digest,
            quota=quota,
            trace_id=trace_id,
            ue_anchor_ref=ue_anchor_ref,
            sent_at=now,
            receipt=receipt,
            previous=previous,
        )
        self._attempts[result_key] = attempt
        return self._evaluate(attempt, now)

    def _evaluate(self, attempt: _Attempt, observed_through: datetime) -> ActuationOutcome:
        quota = attempt.quota
        scope_key = quota.identity.key()
        readback_verified = self._transport.readback(scope_key) == quota.ratios
        measurement_correlated = self._collector.ran_correlated(
            quota.identity,
            attempt.trace_id,
            not_before=attempt.sent_at,
            not_after=observed_through,
        )
        core_evidence_correlated = self._collector.core_correlated(
            quota.identity,
            attempt.trace_id,
            not_before=attempt.sent_at,
            not_after=observed_through,
        )
        enforced = (
            attempt.receipt.acknowledged
            and readback_verified
            and measurement_correlated
            and core_evidence_correlated
        )
        if enforced:
            reason = "RC readback plus correlated RAN and Core slice evidence corroborate the requested quota"
        elif not attempt.receipt.acknowledged:
            reason = "RIC Control Failure"
        elif not readback_verified:
            reason = "ACK received but RC quota readback does not match"
        elif not measurement_correlated:
            reason = "ACK and RC readback received, but no correlated TS 28.552 slice counter exists"
        else:
            reason = "ACK, RC readback, and RAN counter exist, but correlated Core slice evidence is absent"
        outcome = ActuationOutcome(
            policy_type_id=POLICY_TYPE_ID,
            policy_id=attempt.policy_id,
            policy_digest=attempt.policy_digest,
            delivery_acknowledged=attempt.receipt.acknowledged,
            readback_verified=readback_verified,
            measurement_correlated=measurement_correlated,
            core_evidence_correlated=core_evidence_correlated,
            enforced=enforced,
            previous_ratios=attempt.previous,
            reason=reason,
        )
        self._results[(attempt.policy_id, attempt.policy_digest)] = outcome
        if self._producer is not None:
            self._producer.record_status(
                attempt.policy_id,
                delivery_acknowledged=outcome.delivery_acknowledged,
                readback_verified=outcome.readback_verified,
                measurement_correlated=outcome.measurement_correlated,
                core_evidence_correlated=outcome.core_evidence_correlated,
                previous_quota=(
                    outcome.previous_ratios.as_policy()
                    if outcome.previous_ratios is not None else None
                ),
                reason=outcome.reason,
            )
        return outcome

    def rollback(self, policy_id: str, *, fencing_token: int) -> PrbRatios:
        history = self._rollback.get(policy_id)
        if not history:
            raise RollbackError(f"policy {policy_id} has no restorable previous quota")
        scope_key, identity, previous, ue_anchor_ref = history[-1]
        if fencing_token <= self._last_token.get(scope_key, 0):
            raise FencingError("rollback fencing token is stale")
        request = encode_control_request(
            (SliceQuota(identity, previous),), ue_anchor_ref=ue_anchor_ref,
        )
        self._last_token[scope_key] = fencing_token
        receipt = self._transport.send(
            request, scope_key=scope_key, fencing_token=fencing_token,
            idempotency_key=f"rollback:{policy_id}:{fencing_token}",
        )
        if not receipt.acknowledged or self._transport.readback(scope_key) != previous:
            raise RollbackError("previous quota was not restored and read back")
        history.pop()
        if self._producer is not None:
            self._producer.record_rolled_back(policy_id, previous.as_policy())
        return previous

    def delete_policy(self, policy_id: str, *, fencing_token: int) -> PrbRatios:
        """Restore before deleting the A1 resource and releasing scope ownership."""
        if self._producer is None:
            raise WorkerError("delete lifecycle requires the bound A1 producer")
        policy = self._producer.get_policy(POLICY_TYPE_ID, policy_id)
        scope_key = self.scope_key(policy)
        history = self._rollback.get(policy_id)
        if history:
            restored = self.rollback(policy_id, fencing_token=fencing_token)
        else:
            has_attempt = any(key[0] == policy_id for key in self._attempts)
            current = self._transport.readback(scope_key)
            requested = quota_from_policy(policy).ratios
            if not has_attempt or current != requested:
                raise RollbackError(
                    f"policy {policy_id} has no verified no-op or restorable previous quota",
                )
            restored = current
        self._producer._delete_after_rollback(
            POLICY_TYPE_ID, policy_id, self._delete_capability,
        )
        if self._scope_owner.get(scope_key) == policy_id:
            self._scope_owner.pop(scope_key)
        self._rollback.pop(policy_id, None)
        self._attempts = {
            key: value for key, value in self._attempts.items()
            if key[0] != policy_id
        }
        self._results = {
            key: value for key, value in self._results.items()
            if key[0] != policy_id
        }
        return restored

    def _delete_from_a1(self, policy_id: str) -> None:
        if self._producer is None:
            raise WorkerError("A1 DELETE requires the bound producer")
        policy = self._producer.get_policy(POLICY_TYPE_ID, policy_id)
        scope_key = self.scope_key(policy)
        try:
            self.delete_policy(
                policy_id,
                fencing_token=self._last_token.get(scope_key, 0) + 1,
            )
        except Exception as exc:
            raise A1Conflict(f"DELETE rollback gate failed: {exc}") from exc
