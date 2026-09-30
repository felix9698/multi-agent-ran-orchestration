"""Where an actuator binding's behaviour-bearing values come from.

Added for Gate 3; recorded in ``docs/architecture/SEAMS-GATE2.md`` section 8.5.

A gateway command carries the scope, the axis and the value, and nothing else.
The frozen ``AIC_UECellSteering_1.0.0`` policy body needs fourteen more
numbers and identifiers -- deadlines, validity, revisions, a producer, a
correlation -- and its translator states plainly that it has "intentionally no
convenience defaults: the contract forbids silently introducing
behavior-critical defaults".

So every one of them has to come from something the epoch froze.  This module
is that derivation, and it is deliberately *transport-blind*: it produces
values, not a policy body.  Turning an :class:`ActuationRequest` into an A1
policy object is the composition root's job, outside ``assurance/``, because
``assurance/**`` may not import ``oran.rapp`` (the seam test enforces it).

Two rules the body follows and the tests hold it to:

**Nothing is invented.**  Every field traces to a frozen contract, a Kernel
identifier or a permit.  Where a value is computed rather than read, it is
computed by a named rule and the record says which -- ``DERIVED`` values keep
their derivation rule and inputs (design section 6.1).

**Nothing is drawn.**  The identifiers an O-RAN interface types as UUIDs are
derived from Kernel identifiers with
:func:`~assurance.core.addressing.deterministic_uuid`, so the same trial
produces the same policy body on a replay.  A fresh UUID per attempt would put
a random value into the event stream, and the terminal state hash would stop
reproducing on the second run.
"""

from __future__ import annotations

import os
import math
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence, Tuple

from assurance.contracts.capability import ActuatorBinding, ActuatorPath
from assurance.contracts.catalog import Candidate, CoordinationCasePolicy
from assurance.contracts.harm import HarmContract
from assurance.contracts.measurement import MeasurementContract
from assurance.contracts.target import TargetContract
from assurance.core.addressing import deterministic_uuid
from assurance.core.components import ComponentId
from assurance.core.timebase import is_utc_timestamp

__all__ = [
    "ACTUATION_DERIVATION_RULES",
    "ActuationRequest",
    "derive_actuation_request",
]

#: The named rules this module computes a value by, rather than reading it.
#: Recorded on the request so a value that was worked out can be told from a
#: value that was frozen, and so a reader can re-run the arithmetic.
ACTUATION_DERIVATION_RULES: Mapping[str, str] = {
    "actionDeadlineMs": "harm_bound_enforced_timeout",
    "rollbackTimeoutMs": "harm_bound_enforced_timeout",
    "minSecondsBetweenActuations": "harm_bound_enforced_timeout_ceiling_seconds",
    "requiredKpiFreshnessMs": "strictest_frozen_measurement_freshness",
}


class ActuationDerivationError(ValueError):
    """A behaviour-bearing value has no frozen source.

    Raised rather than defaulted.  A deadline nobody contracted is not a
    deadline, and the one thing worse than refusing to build a policy body is
    building one whose safety numbers were made up here.
    """


@dataclass(frozen=True)
class ActuationRequest:
    """Every behaviour-bearing value one actuation needs, and where it came from.

    Transport-blind on purpose: these are contract values, and the shapes an
    interface wants them in -- an ``AIC_UECellSteering_1.0.0`` ``UeId``, a
    ``CId`` with an integer ``ncI`` -- belong to the binding that speaks it.

    Attributes
    ----------
    objective_family / scope_selector / parameters:
        What is being asked for, for whom, and with which values.  All three
        come from the epoch-frozen target and the frozen catalog candidate;
        an advisory names the candidate and nothing here.
    action_deadline_ms / rollback_timeout_ms / min_seconds_between_actuations:
        The enforced runtime window.  Derived from the harm bound's
        ``enforced_timeout_ms`` -- "the runtime timeout that *makes* the bound
        true, not a hope that it will be".  A second actuation inside that
        window would start before the bound that justified the first one had
        its chance to hold, which is why the pacing floor is the same number.
    required_kpi_freshness_ms:
        The strictest freshness bound among the epoch's measurement
        contracts.  Strictest rather than the one for this predicate: the
        policy carries a single number, and the safe reading of a single
        number is the tightest the epoch requires.
    not_before / expires_at:
        The permit's own life.  The policy must not outlive the permit that
        authorised it, so validity is the lease rather than a second clock.
    intent_id / correlation_id:
        Deterministic UUIDs over the case (and optional downstream policy
        scope) and trial identifiers. Joint policies retain the shared trial
        correlation while each policy has its own stable intent identity.
    producer_id:
        The Kernel.  Routing and correlation only -- never an authority
        (design section 4.8).
    intent_revision / policy_revision:
        The case's revision and this trial's position within it, so a retry is
        a new revision rather than an indistinguishable repeat.
    idempotency_key:
        What the *downstream* deduplicates on, derived from the same Kernel
        identifiers as the gateway's own per-command key.  The two live in
        different namespaces and must not be confused, but they are
        correlated by construction rather than by luck.
    derivation_rules:
        Field name to the rule that computed it, for the fields that were
        computed.  Read fields are absent.
    """

    objective_family: str
    scope_selector: Mapping[str, str]
    parameters: Mapping[str, str]
    policy_type_id: str
    actuator_path: ActuatorPath
    deployment_binding_ref: str
    action_deadline_ms: int
    rollback_timeout_ms: int
    min_seconds_between_actuations: int
    required_kpi_freshness_ms: int
    not_before: str
    expires_at: str
    intent_id: str
    correlation_id: str
    producer_id: str
    intent_revision: int
    policy_revision: int
    idempotency_key: str
    derivation_rules: Mapping[str, str]

    def to_canonical_dict(self) -> dict:
        """Canonical form, for the event record and for comparison."""
        return {
            "actionDeadlineMs": self.action_deadline_ms,
            "actuatorPath": self.actuator_path.value,
            "correlationId": self.correlation_id,
            "deploymentBindingRef": self.deployment_binding_ref,
            "derivationRules": dict(self.derivation_rules),
            "expiresAt": self.expires_at,
            "idempotencyKey": self.idempotency_key,
            "intentId": self.intent_id,
            "intentRevision": self.intent_revision,
            "minSecondsBetweenActuations": self.min_seconds_between_actuations,
            "notBefore": self.not_before,
            "objectiveFamily": self.objective_family,
            "parameters": dict(self.parameters),
            "policyRevision": self.policy_revision,
            "policyTypeId": self.policy_type_id,
            "producerId": self.producer_id,
            "requiredKpiFreshnessMs": self.required_kpi_freshness_ms,
            "rollbackTimeoutMs": self.rollback_timeout_ms,
            "scopeSelector": dict(self.scope_selector),
        }


#: objective bundle 이 harm bound 에 싣는 기본 한도.  이 값이 정책의
#: ``actionDeadlineMs`` / ``rollbackTimeoutMs`` 가 되고, 프로듀서는 그 시간이 지나면
#: 액션을 실패로 본다.
#:
#: **10초는 실측 확증 시간보다 짧다.**  2026-09-23 실기 측정: 조종 정책 하나를 혼자
#: 쏘았을 때 KPM 이 목표 셀을 보기까지 6.0초, 프로듀서 readback 이 VERIFIED 가 되기까지
#: **7.8초**.  여유가 2.2초뿐인데 판은 조종을 혼자 쓰지 않는다 -- 9축을 한 트랜잭션으로
#: 쓰므로 앞의 축들이 그 여유를 먹고, 조종 시행이 매번
#: ``PARTIAL_APPLY 9/9 acknowledged (readback confirmed only 2)`` 로 판정돼 롤백됐다.
#: 그 롤백이 절반쯤 넘어간 UE 를 붕 띄웠다.
DEFAULT_ENFORCED_TIMEOUT_MS = 10_000

#: 그 한도를 덮는 환경 이름.  `tools/liveconsole/agent.py` 의 `LiveTiming` 덮어쓰기와
#: **같은 이름을 쓴다** -- 한 판에서 두 경로가 다른 한도를 쓰면 무엇이 시간을 먹었는지
#: 영원히 못 가린다.
ENFORCED_TIMEOUT_ENV = "AIC_ENFORCED_TIMEOUT_MS"


def enforced_timeout_ms_default() -> int:
    """harm bound 에 실을 한도.  환경이 주면 그 값, 아니면 선언된 기본값.

    잘못된 값은 **조용히 기본값으로 돌아가지 않는다** -- 운영자는 자기가 준 값이 쓰인 줄
    알게 되고, 그러면 한도가 왜 짧은지 다시 며칠을 찾게 된다.
    """
    raw = os.environ.get(ENFORCED_TIMEOUT_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_ENFORCED_TIMEOUT_MS
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise ValueError(
            f"{ENFORCED_TIMEOUT_ENV} must be a whole number of milliseconds, got {raw!r}")
    if value <= 0:
        raise ValueError(f"{ENFORCED_TIMEOUT_ENV} must be positive, got {value}")
    return value


def _enforced_timeout_ms(harm: HarmContract) -> int:
    """The shortest enforced timeout among the harm contract's bounds."""
    timeouts = [
        int(bound.enforced_timeout_ms)
        for bound in harm.bounds
        if int(getattr(bound, "enforced_timeout_ms", 0)) > 0
    ]
    if not timeouts:
        raise ActuationDerivationError(
            f"harm contract {harm.contract_id!r} states no enforced timeout; "
            "there is no contracted action deadline to actuate under"
        )
    return min(timeouts)


def _strictest_freshness_ms(measurements: Sequence[MeasurementContract]) -> int:
    bounds = [
        int(measurement.freshness_bound_ms)
        for measurement in measurements
        if int(getattr(measurement, "freshness_bound_ms", 0)) > 0
    ]
    if not bounds:
        raise ActuationDerivationError(
            "no frozen measurement contract states a freshness bound; "
            "the policy would carry a KPI freshness nobody contracted"
        )
    return min(bounds)


def derive_actuation_request(
    *,
    target: TargetContract,
    candidate: Candidate,
    actuator: ActuatorBinding,
    harm: HarmContract,
    measurements: Sequence[MeasurementContract],
    case_policy: CoordinationCasePolicy,
    case_id: str,
    trial_id: str,
    trial_index: int,
    issued_at: str,
    lease_expiry: str,
    policy_scope: str | None = None,
) -> ActuationRequest:
    """Derive one actuation's behaviour-bearing values from the frozen epoch.

    Pure: same frozen contracts and same Kernel identifiers, same request,
    including the UUIDs.  Refuses rather than defaults -- every raise below is
    a value the deployment's contracts do not state, and a policy body built
    without it would be carrying a number this function invented.
    """
    if not isinstance(target, TargetContract):
        raise ActuationDerivationError("a target contract is required")
    if not isinstance(candidate, Candidate):
        raise ActuationDerivationError("a frozen catalog candidate is required")
    if not isinstance(actuator, ActuatorBinding):
        raise ActuationDerivationError("an actuator binding is required")
    if actuator.path is not ActuatorPath.OFFICIAL_ORAN_DYNAMIC:
        raise ActuationDerivationError(
            f"{actuator.contract_id!r} declares {actuator.path.value}; only the "
            "official dynamic path may carry an objective effect (design section 9)"
        )
    if actuator.capability_ref != candidate.capability_ref:
        raise ActuationDerivationError(
            "the actuator binding does not serve the candidate's capability"
        )
    if candidate.target_ref != target.contract_id:
        raise ActuationDerivationError(
            "the candidate does not instantiate this target contract"
        )
    if harm.contract_id not in set(case_policy.harm_contract_refs):
        raise ActuationDerivationError(
            "the harm contract is not one this case runs under"
        )
    if not is_utc_timestamp(issued_at) or not is_utc_timestamp(lease_expiry):
        raise ActuationDerivationError("permit instants must be canonical UTC")
    if trial_index < 1:
        raise ActuationDerivationError("a trial position starts at 1")

    enforced_timeout_ms = _enforced_timeout_ms(harm)
    freshness_ms = _strictest_freshness_ms(measurements)
    # A joint Kernel case installs independent downstream policies. Preserve
    # each policy's identity across trials without deduplicating different UEs
    # against one another. The unscoped single-policy case keeps its identity.
    intent_id = deterministic_uuid(
        case_id if policy_scope is None else json.dumps(
            [case_id, policy_scope], separators=(",", ":")))
    correlation_id = deterministic_uuid(f"{case_id}:{trial_id}")
    return ActuationRequest(
        objective_family=target.objective_family,
        scope_selector=dict(target.scope_selector),
        parameters=dict(candidate.parameters),
        policy_type_id=actuator.policy_type_id,
        actuator_path=actuator.path,
        deployment_binding_ref=actuator.deployment_binding_ref,
        action_deadline_ms=enforced_timeout_ms,
        rollback_timeout_ms=enforced_timeout_ms,
        min_seconds_between_actuations=int(
            math.ceil(enforced_timeout_ms / 1000.0)
        ),
        required_kpi_freshness_ms=freshness_ms,
        not_before=issued_at,
        expires_at=lease_expiry,
        intent_id=intent_id,
        correlation_id=correlation_id,
        producer_id=ComponentId.ASSURANCE_KERNEL.value,
        intent_revision=1,
        policy_revision=trial_index,
        idempotency_key=f"{intent_id}:{trial_index}",
        derivation_rules=dict(ACTUATION_DERIVATION_RULES),
    )
