"""The deterministic Assurance Kernel facade.

Owner lane: **KERN**.  Signatures frozen by this design step.

Design section 4.3 gives this one component exclusive ownership of:

    contract and deployment admission; evidence epoch creation and freeze;
    finite candidate-catalog generation and hashing; trial state transitions
    and transaction fencing; harm reservation, watchdog thresholds, charging,
    and settlement; raw-trace evaluation and evidence closure; candidate
    exhaustion, target-vector release, and case termination; crash recovery
    and append-only event reduction.

The method list below is that sentence, one method per clause.  Nothing else
in the system may do any of it -- design acceptance criterion 17.1: "LLMs
cannot directly determine targets, catalogs, authorization, verdicts, ledgers,
harm, release, or termination."

Two properties every method shares, so the individual docstrings do not repeat
them:

**Deterministic.**  Same event stream and same reducer version, same result.
No clock reads outside what arrives in an envelope, no randomness, no model
call, no network read that is not an explicitly passed-in observation.  Every
method that needs "now" takes it as a parameter for exactly this reason.

**Event-sourced.**  A method's effect *is* the event it appends.  There is no
Kernel state that is not reconstructible by replaying the stream, which is
what makes crash recovery (design section 8) a replay rather than a guess.

What the Kernel deliberately does **not** do: it never opens a transport, never
sends an actuator command, and never talks to a model.  It issues a
:class:`~assurance.gateway.token.KernelToken` and the Write Gateway acts; it
accepts a typed advisory and decides for itself.
"""

from __future__ import annotations

from copy import deepcopy
from types import MappingProxyType
from dataclasses import fields, is_dataclass, replace
from datetime import timedelta
from enum import Enum
from threading import RLock
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from assurance.contracts.catalog import (
    Candidate,
    CandidateCatalog,
    CoordinationCasePolicy,
    DomainMembership,
    domain_candidate,
    generate_catalog,
    membership_digests,
)
from assurance.contracts.epoch import EpochRecord, epoch_hash, freeze_epoch as build_epoch
from assurance.contracts.harm import HarmKind
from assurance.contracts.ledgers import (
    CompatibilityCheck,
    CompatibilityRecord,
    EvidenceCell,
    EvidenceContribution,
)
from assurance.contracts.target import TargetVector
from assurance.contracts.validation import (
    assert_secret_free,
    canonical_form,
    contract_content_hash,
    validate_contract,
    validate_family_set,
)
from assurance.core.addressing import content_hash, is_content_hash
from assurance.core.axes import (
    AggregateState,
    CandidateAvailability,
    CaseTermination,
    EvidenceCellStatus,
    ExecutionValidity,
    MeasurementSufficiency,
    OPEN_EVIDENCE_CELL_STATUSES,
    PredicateVerdict,
    TrialOutcome,
    aggregate_from_cells,
    counts_toward_closure,
)
from assurance.core.confirmation import ConfirmationRecord
from assurance.core.components import ADVISORY_COMPONENTS, ComponentId
from assurance.core.envelopes import (
    ASSURANCE_SCHEMA_VERSION,
    EnvelopeAdmissionState,
    EnvelopeRejection,
    EventEnvelope,
    MailboxEnvelope,
    classify_envelope,
)
from assurance.core.states import StopReason, TrialState
from assurance.core.states import assert_transition, strongest_reason
from assurance.core.timebase import format_utc, parse_utc
from assurance.gateway.token import KernelToken, TokenKind
from assurance.gateway.gateway import WATCHDOG_HOSTING_PREFIX
from assurance.gateway.write_gateway import GatewayOutcome, GatewayResult
from assurance.kernel.reducer import KernelReducer, replay, terminal_state_hash as hash_terminal_state

__all__ = [
    "KERNEL_HOSTED_WATCHDOG_GUARDS",
    "AssuranceKernel",
    "KernelRefusal",
]

#: The two guards the Kernel hosts end to end when the deployment behind
#: an adapter cannot host a contract watchdog itself.  Both already exist
#: and both are exercised by the Gate 2 fault suite; naming them makes the
#: arming record say which mechanisms stood in.
KERNEL_HOSTED_WATCHDOG_GUARDS: Tuple[str, ...] = (
    "measurement-staleness",
    "token-lease-deadline",
)


def _plain(value: Any) -> Any:
    """Convert frozen dataclass values into deterministic JSON data."""
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _candidate_payload(candidate: Candidate) -> Dict[str, Any]:
    return {
        "candidateId": candidate.candidate_id,
        "targetRef": candidate.target_ref,
        "optionRef": candidate.option_ref,
        "parameters": dict(candidate.parameters),
        "semanticHash": candidate.semantic_hash,
        "capabilityRef": candidate.capability_ref,
    }


def _catalog_payload(catalog: CandidateCatalog) -> Dict[str, Any]:
    domain = catalog.is_domain
    return {
        "catalogHash": catalog.catalog_hash,
        "cardinality": catalog.cardinality,
        # 2026-09-19: a frozen domain is carried as its declaration; the points
        # are decoded on demand (``frozen_candidate``), never listed.
        "candidates": ([] if domain else
                       [_candidate_payload(candidate) for candidate in catalog.candidates]),
        **({"domain": catalog.candidates.declaration()} if domain else {}),
        "contractId": catalog.contract_id,
        "documentStatus": catalog.document_status,
        "epochRef": catalog.epoch_ref,
        "generatorVersion": catalog.generator_version,
        "schemaVersion": catalog.schema_version,
        "standardMapping": dict(catalog.standard_mapping),
        "version": catalog.version,
    }


def frozen_candidate(catalog: Mapping[str, Any], candidate_id: Any) -> Optional[Dict[str, Any]]:
    """The frozen catalog's candidate ``candidate_id`` as a payload row, or ``None``.

    One lookup for both shapes: an enumerated catalog is scanned, a domain is
    decoded in time independent of its size.
    """
    if not catalog:
        return None
    if catalog.get("domain"):
        found = domain_candidate(catalog["domain"], str(candidate_id))
        return None if found is None else _candidate_payload(found)
    return next((item for item in catalog.get("candidates", [])
                 if item.get("candidateId") == candidate_id), None)


def candidate_availability(state: Mapping[str, Any], candidate_id: Any) -> Optional[str]:
    """A candidate's availability: recorded, or ``AVAILABLE`` for an untouched
    member of a frozen domain (a domain records only the points it has used)."""
    recorded = state["candidateAvailability"].get(candidate_id)
    if recorded is not None:
        return recorded
    catalog = state.get("catalog") or {}
    if catalog.get("domain") and frozen_candidate(catalog, candidate_id) is not None:
        return CandidateAvailability.AVAILABLE.value
    return None


def _epoch_payload(record: EpochRecord, *, digest: str) -> Dict[str, Any]:
    return {
        "actuatorBindingHashes": dict(record.actuator_binding_hashes),
        "candidateGeneratorVersion": record.candidate_generator_version,
        "candidateSemanticHashes": list(record.candidate_semantic_hashes),
        "candidateUniverseCardinality": record.candidate_universe_cardinality,
        "candidateDomain": bool(getattr(record, "candidate_domain", False)),
        "capabilityManifestHashes": dict(record.capability_manifest_hashes),
        "casePolicyHash": record.case_policy_hash,
        "catalogHash": record.catalog_hash,
        "compositionManifestHash": record.composition_manifest_hash,
        "confirmationRef": record.confirmation_ref,
        "counterBindingHashes": dict(record.counter_binding_hashes),
        "deploymentBindingHashes": dict(record.deployment_binding_hashes),
        "epochHash": digest,
        "epochId": record.epoch_id,
        "evaluatorVersion": record.evaluator_version,
        "frozenAt": record.frozen_at,
        "harmContractHashes": dict(record.harm_contract_hashes),
        "measurementContractHashes": dict(record.measurement_contract_hashes),
        "reducerVersion": record.reducer_version,
        "supersedesEpochRef": record.supersedes_epoch_ref,
        "targetContractHashes": dict(record.target_contract_hashes),
        "targetVectorHash": record.target_vector_hash,
        "targetVectorOrder": list(record.target_vector_order),
    }


def _cell_payload(cell: EvidenceCell, *, case_id: str, epoch_id: str) -> Dict[str, Any]:
    return {
        "candidateSemanticHash": cell.candidate_semantic_hash,
        "caseId": case_id,
        "cellId": cell.cell_id,
        "contributions": [_contribution_payload(item) for item in cell.contributions],
        "epochId": epoch_id,
        "requiredIndependentContributions": cell.required_independent_contributions,
        "sealedUntilVectorRef": cell.sealed_until_vector_ref,
        "status": cell.status.value,
        "targetRef": cell.target_ref,
    }


def _contribution_payload(contribution: EvidenceContribution) -> Dict[str, Any]:
    return {
        "candidateSemanticHash": contribution.candidate_semantic_hash,
        "contributionId": contribution.contribution_id,
        "dependencyGroup": contribution.dependency_group,
        "executionValidity": contribution.execution_validity.value,
        "isPostClosureWitness": contribution.is_post_closure_witness,
        "measurementSufficiency": contribution.measurement_sufficiency.value,
        "predicateVerdict": contribution.predicate_verdict.value,
        "reusedFromEpoch": contribution.reused_from_epoch,
        "traceRefs": list(contribution.trace_refs),
        "trialRef": contribution.trial_ref,
    }


class KernelRefusal(RuntimeError):
    """The Kernel refused an operation.

    Carries the reason as data so the Cockpit can display it and the paper can
    count it (design section 13 reports "proposal rejection/staleness,
    fallback usage, and invariant violations").  A refusal is a normal,
    recorded outcome -- it is appended to the stream like anything else, not
    an exceptional condition that disappears into a log.
    """

    def __init__(self, reason: str, *, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


#: Lease for a permit whose effect is a single point-in-time operation.
#: Long enough to absorb transport and retry, short enough that a stalled
#: gateway is found by the watchdog rather than waited on.
DEFAULT_LEASE_MS = 30_000


def contracted_lease_ms(
    state: Mapping[str, Any], trial: Mapping[str, Any], token_kind: Any
) -> int:
    """How long this permit must live, from the frozen contracts alone.

    Most gateway operations are instantaneous, so the default lease is the
    right one.  ``COMMIT`` is not: the change it authorises has to stay
    applied through the contracted hold and the configuration reread that
    follows it, and the lease is what the gateway watchdogs against.  A
    lease shorter than that window would either expire mid-hold -- which
    the Kernel reads as ``StopReason.LEASE_EXPIRY`` and therefore
    ``ExecutionValidity.INVALID`` -- or invite a downstream component to
    quietly extend it, which would let a policy outlive the authorisation
    it was issued under.  So the window is priced here, where the
    authorisation is granted, and the recorded expiry is the only expiry.

    Derived from frozen contracts only: the longest hold the trial's target
    and measurement contracts require, plus two of the strictest action
    deadline the epoch's harm bounds enforce -- one for the reread and one
    for the finalise that follows the hold.  Anything the Kernel cannot
    resolve leaves the default in place; the composed request is then
    refused downstream for naming a window its permit does not cover,
    which is the fail-closed direction.
    """

    if token_kind in (TokenKind.STOP, TokenKind.REVERSE_ROLLBACK):
        return _per_step_lease_ms(state, trial)
    if (token_kind in (TokenKind.CONFIGURATION_REREAD, TokenKind.RECOVERY_CONFIRM)
            and trial.get("state") == TrialState.RECOVERY_VERIFYING.value):
        # 2026-09-23 (board 459): ``recover`` is one call at one ``now``, so the
        # reread and confirmation after a reversal are stamped with the instant
        # the reversal *started*.  That reversal ran 61 s; both permits (30 s)
        # were already expired on arrival and the case locked down over an ACKED
        # rollback.  Their window must span the reversal they follow.
        return _per_step_lease_ms(state, trial) + DEFAULT_LEASE_MS
    if token_kind in _READ_BACK_KINDS:
        return _per_read_lease_ms(trial)
    if token_kind is not TokenKind.COMMIT:
        return DEFAULT_LEASE_MS
    epoch = state["epochs"].get(trial.get("epochId"))
    if not epoch:
        return DEFAULT_LEASE_MS

    def _body(family: str, contract_hash: Any) -> Mapping[str, Any]:
        record = state["contracts"].get(contract_hash) or {}
        if record.get("family") != family:
            return {}
        return record.get("body") or {}

    def _int(source: Mapping[str, Any], *names: str) -> int:
        for name in names:
            if name in source:
                try:
                    return int(source[name])
                except (TypeError, ValueError):
                    return 0
        return 0

    holds = []
    target = _body(
        "TargetContract",
        epoch.get("targetContractHashes", {}).get(trial.get("targetRef")),
    )
    holds.append(_int(target, "hold_ms", "holdMs"))
    for contract_hash in epoch.get("measurementContractHashes", {}).values():
        holds.append(
            _int(_body("MeasurementContract", contract_hash), "hold_ms", "holdMs")
        )
    hold_ms = max(holds) if holds else 0
    if hold_ms <= 0:
        return DEFAULT_LEASE_MS

    strictest = _strictest_action_deadline_ms(state, epoch)
    if not strictest:
        return DEFAULT_LEASE_MS
    # 2026-09-24 board 762: the hold starts only after the apply is read back, one
    # participant after another (a ue2/ue3 swap: writes done 7 s in, then the steering
    # readbacks), so a lease of hold + 2 x 10 s expired before the result arrived.
    steps = max(0, len(trial.get("planPrefixHashes") or ()) - 1)
    apply_ms = (steps + 1) * R1_CALL_BOUND_MS if steps else 0
    return max(DEFAULT_LEASE_MS, hold_ms + 2 * strictest + apply_ms)


def _strictest_action_deadline_ms(state: Mapping[str, Any], epoch: Mapping[str, Any]) -> int:
    """The smallest ``enforcedTimeoutMs`` the epoch's harm bounds name, or 0."""
    deadlines = []
    for contract_hash in epoch.get("harmContractHashes", {}).values():
        record = state["contracts"].get(contract_hash) or {}
        if record.get("family") != "HarmContract":
            continue
        for bound in (record.get("body") or {}).get("bounds", []):
            if not isinstance(bound, Mapping):
                continue
            for name in ("enforced_timeout_ms", "enforcedTimeoutMs"):
                if name in bound:
                    try:
                        value = int(bound[name])
                    except (TypeError, ValueError):
                        value = 0
                    if value > 0:
                        deadlines.append(value)
                    break
    return min(deadlines) if deadlines else 0


def _plan_subset_hashes(event_store: Any, trial_id: str) -> frozenset:
    """Every own-partial-apply digest of the plan staged for *trial_id*.

    Read from the ``TrialPlanStaged`` event itself, **not** added to the reduced
    trial: a new reduced key would change the terminal state hash of every
    recorded board and refuse their replay (2026-09-22, reducer 1.2.0).
    """
    from assurance.gateway.plan import ActuationPlan, PlanError
    plan = None
    for envelope in event_store.iterate():
        if (envelope.event_kind == "TrialPlanStaged"
                and envelope.payload.get("trialId") == trial_id):
            plan = envelope.payload.get("plan")
    if not plan:
        return frozenset()
    try:
        return ActuationPlan.from_mapping(plan).subset_hashes()
    except (PlanError, KeyError, TypeError, ValueError):
        return frozenset()


#: One live R1 call to its bound.  Not in any frozen contract: the client timeout
#: is the composition's ``r1.timeoutSeconds`` (default 20.0,
#: ``tools/g3ota/composition.py``) and the corroborated readback polls to the
#: deployment binding's ``r1.deadlineMs`` (20000).  Measured 2026-09-22/23: the
#: slow reversals sat at 20.0-20.3 s per call (x6), so 20.5 s.
R1_CALL_BOUND_MS = 20_500

#: The worst single reversal step, written out term by term (2026-09-23 audit).
#: A steering UNDO is a hand-back UPDATE, the corroborated readback, the
#: independent read when that one does not see the baseline, then the DELETE --
#: four calls to the R1 bound -- plus the producer's identity gate, re-asked up to
#: 4 times 2 s apart (``R1Adapter.GATE_RETRY_ATTEMPTS``/``GATE_RETRY_WAIT_S``).
#: The strictest action deadline (10 s live) prices none of that, so a 9-step
#: reversal was leased 100 s while one steering step alone may take 90.
R1_UNDO_STEP_BOUND_MS = 4 * R1_CALL_BOUND_MS + 4 * 2_000


#: Every permit whose gateway operation reads the participants back (``_observe``)
#: and is otherwise leased as a point-in-time operation.  2026-09-24 board 761:
#: READY, leased 30 s after the PREPARE fix went in, expired the same way.
_READ_BACK_KINDS = (TokenKind.PREPARE, TokenKind.READY, TokenKind.CONFIGURATION_REREAD,
                    TokenKind.RECOVERY_CONFIRM, TokenKind.FINALIZE_LIVE)


def _per_read_lease_ms(trial: Mapping[str, Any]) -> int:
    """``PREPARE`` and a plain reread read every participant back, one after another.

    2026-09-24 board 758: trial 2's PREPARE was leased 30 s; the gateway's
    ``_observe`` read the joint plan's participants serially and the first
    VALIDATE left 60.1 s after the permit -- three participant reads run to the
    20 s R1 bound -- so the Kernel refused the prepare as ``LEASE_EXPIRED`` and
    ended the case with 6 of 8 trials unspent.  Nothing is written under either
    permit, so a lease long enough to finish the reads costs no safety; one that
    is too short ends the board.  One R1 call bound per staged step plus one.
    """
    steps = max(0, len(trial.get("planPrefixHashes") or ()) - 1)
    if steps <= 0:
        return DEFAULT_LEASE_MS
    return max(DEFAULT_LEASE_MS, (steps + 1) * R1_CALL_BOUND_MS)


def _per_step_lease_ms(state: Mapping[str, Any], trial: Mapping[str, Any]) -> int:
    """``STOP`` and ``REVERSE_ROLLBACK`` act once **per staged step**, then read.

    2026-09-23: these two were leased like a point-in-time operation (30 s)
    on the premise that "the rest are instantaneous".  Two days of live
    boards refute it: over 208 reversals p50 is 0.0 s but p99 is 40.5 s and
    the maximum 81.3 s, and the slow ones sit at whole multiples of the R1
    call timeout (20.0-20.3 s x6, 40.1, 40.5, 61.1, 81.3) -- each a
    participant's call that ran to its bound, serially across a 9-step joint
    plan.  Four reversals outlived their own permit; the Kernel then refused
    the result as ``LEASE_EXPIRED`` and ended the case (7 boards
    ``KERNEL_TERMINATED`` with budget left).  A safety action whose permit
    expires before the action can finish is self-defeating: the case stops
    with the reversal unconfirmed and our policies possibly still at the RIC.

    Priced the way COMMIT is -- from frozen contracts, where the permit is
    granted: one step price per staged step plus one for the confirming read.
    Never below the default.  An unresolvable plan or harm contract keeps the
    default (fail closed, as before).

    The step price is the strictest action deadline, but never below
    :data:`R1_UNDO_STEP_BOUND_MS`: the contract names how long the *radio* may
    take, not how long the R1 calls that ask it may run, and pricing the second
    by the first under-leased every reversal that ran into the call bound.
    """
    epoch = state["epochs"].get(trial.get("epochId"))
    steps = max(0, len(trial.get("planPrefixHashes") or ()) - 1)
    if not epoch or steps <= 0:
        return DEFAULT_LEASE_MS
    strictest = _strictest_action_deadline_ms(state, epoch)
    if not strictest:
        return DEFAULT_LEASE_MS
    return max(DEFAULT_LEASE_MS, (steps + 1) * max(strictest, R1_UNDO_STEP_BOUND_MS))


class AssuranceKernel:
    """The single decision authority of the assurance system.

    Constructed with an :class:`~assurance.kernel.event_store.EventStore`, a
    :class:`~assurance.kernel.reducer.Reducer`, a
    :class:`~assurance.gateway.write_gateway.WriteGateway` and a
    :class:`~assurance.collector.collector.MeasurementCollector`.  All four are
    injected rather than constructed here, so the Gate 2 hardware-free vertical
    path runs the real Kernel against replay adapters -- the Kernel does not
    know or care whether the gateway underneath it reaches a testbed.
    """

    def __init__(
        self,
        *,
        event_store: Any,
        reducer: Any,
        write_gateway: Any,
        measurement_collector: Any,
    ) -> None:
        """Signature frozen; body owned by lane **KERN**."""
        if all(
            item is None
            for item in (event_store, reducer, write_gateway, measurement_collector)
        ):
            raise NotImplementedError(
                "owned by lane KERN; see docs/architecture/SEAMS-GATE2.md"
            )
        required_store_methods = (
            "append",
            "iterate",
            "last_position",
            "last_sequence",
            "has_event_id",
            "idempotency_hash",
            "uncertain_transactions",
        )
        if any(not callable(getattr(event_store, name, None)) for name in required_store_methods):
            raise TypeError("event_store does not implement the EventStore protocol")
        if not callable(getattr(reducer, "initial_state", None)) or not callable(
            getattr(reducer, "apply", None)
        ):
            raise TypeError("reducer does not implement the Reducer protocol")
        if not isinstance(getattr(reducer, "reducer_version", None), str):
            raise TypeError("reducer must declare reducer_version")
        self._event_store = event_store
        self._reducer = reducer
        self._write_gateway = write_gateway
        self._measurement_collector = measurement_collector
        self._pending_contract_objects: Dict[str, Any] = {}
        self._pending_deployment_objects: Dict[str, Any] = {}
        self._replay_state: Optional[Dict[str, Any]] = None
        self._replay_position = 0
        self._replay_lock = RLock()

    def _state(self) -> Mapping[str, Any]:
        if not hasattr(self, "_event_store") or not hasattr(self, "_reducer"):
            raise NotImplementedError(
                "owned by lane KERN; see docs/architecture/SEAMS-GATE2.md"
            )
        if type(self._reducer) is not KernelReducer:
            return replay(self._reducer, self._event_store.iterate())
        with self._replay_lock:
            # 2026-09-22: 이 줄이 판을 먹고 있었다.  질의마다 커널 상태 전체를 deepcopy
            # 하는데 kernel.py 안에서만 48곳이 이것을 부르고, 상태에는 후보 카탈로그
            # (이 베드에서 192~1152개)·시행·샘플·증거가 들어 있다.  한 시행을 처리하는
            # 동안 시팅이 CPU 19분 27초를 태웠고 판이 통째로 멎은 것처럼 보였다.
            # 스트림은 append-only 이므로 **replay 위치가 그대로면 상태도 그대로다**.
            # 그 사이의 사본은 서로 구분되지 않으므로 하나만 만들어 나눠 쓴다.
            # kernel.py 의 25개 대입 지점 중 스냅샷을 변형하는 곳은 0개다(측정).
            # 공개 경로 `reduced_state()` 는 "독립 스냅샷" 을 약속하므로 거기서 따로 복사한다.
            state = self._current_state()
            position = self._replay_position
            cached = getattr(self, "_snapshot_cache", None)
            if cached is not None and cached[0] == position:
                return cached[1]
            # codex 감사 2026-09-22: 이 사본은 여러 내부 독자가 나눠 쓴다.  지금은 아무도
            # 고치지 않지만(측정), 그건 보장이 아니다.  최상위를 읽기 전용으로 감싸
            # `state["trials"] = ...` 같은 실수를 즉시 실패로 만든다.  중첩 dict 까지
            # 얼리지는 않는다 -- 그 비용이 바로 이 캐시가 없애려던 비용이다.
            snapshot = MappingProxyType(deepcopy(state))
            self._snapshot_cache = (position, snapshot)
            return snapshot

    def _current_state(self) -> Mapping[str, Any]:
        """Private, borrowed state; callers must neither retain nor mutate it.

        The store is append-only.  Fold only its newly accepted suffix, and
        expose independent snapshots through ``_state`` / ``reduced_state``.
        An injected reducer keeps its original protocol/replay behaviour.
        """
        if type(self._reducer) is not KernelReducer:
            return replay(self._reducer, self._event_store.iterate())
        with self._replay_lock:
            return self._refresh_replay_state()

    def _refresh_replay_state(self) -> Mapping[str, Any]:
        """Advance the exclusively owned accumulator under its lock."""
        if self._replay_state is None:
            self._replay_state = dict(self._reducer.initial_state())
            self._replay_position = 0
        try:
            for envelope in self._event_store.iterate(
                since_position=self._replay_position
            ):
                self._reducer._apply_owned(self._replay_state, envelope)
                self._replay_position += 1
        except Exception:
            # An event can fail after partial writes.  Never reuse a poisoned
            # accumulator or skip the rejected event on the next query.
            self._replay_state = None
            self._replay_position = 0
            raise
        return self._replay_state

    def _ensure_initialized(self) -> None:
        if not hasattr(self, "_event_store") or not hasattr(self, "_reducer"):
            raise NotImplementedError(
                "owned by lane KERN; see docs/architecture/SEAMS-GATE2.md"
            )

    def _append(
        self,
        event_kind: str,
        *,
        object_id: str,
        now: str,
        payload: Mapping[str, Any],
        idempotency_key: Optional[str] = None,
    ) -> EventEnvelope:
        sequence = self._event_store.last_sequence(object_id) + 1
        envelope = EventEnvelope.seal(
            schema_version=ASSURANCE_SCHEMA_VERSION,
            object_id=object_id,
            event_id=f"{object_id}:{sequence}:{event_kind}",
            timestamp=now,
            sequence=sequence,
            idempotency_key=(
                idempotency_key
                if idempotency_key is not None
                else f"kernel:{object_id}:{sequence}:{event_kind}"
            ),
            source_component=ComponentId.ASSURANCE_KERNEL,
            event_kind=event_kind,
            payload=payload,
        )
        self._event_store.append(envelope)
        return envelope

    def reduced_state(self) -> Mapping[str, Any]:
        """Return an independent snapshot of the complete accepted stream.

        ``_state`` now hands internal readers a shared read-only snapshot, so
        the independence this method promises is made here (2026-09-22).
        그 뷰는 `MappingProxyType` 이라 그대로 deepcopy 할 수 없다 -- dict 로 풀어 복사한다.
        """
        return deepcopy(dict(self._state()))

    def activate_frozen_epoch(
        self,
        record: EpochRecord,
        catalog: CandidateCatalog,
        *,
        now: str,
        safety_relevant: bool = True,
    ) -> EventEnvelope:
        """Activate KCON-produced frozen data without re-running KCON bodies.

        This boundary is intentionally useful to replay and hardware-free
        fixtures: the dataclasses are already complete and content-addressed.
        A safety-relevant replacement remains drain-gated.
        """
        if not isinstance(record, EpochRecord) or not isinstance(catalog, CandidateCatalog):
            raise TypeError("record and catalog must be frozen KCON dataclasses")
        if record.reducer_version != self._reducer.reducer_version:
            raise KernelRefusal("REDUCER_VERSION_MISMATCH")
        if record.catalog_hash != catalog.catalog_hash:
            raise KernelRefusal("CATALOG_EPOCH_MISMATCH")
        if record.candidate_universe_cardinality != catalog.cardinality:
            raise KernelRefusal("CATALOG_CARDINALITY_MISMATCH")
        if tuple(record.candidate_semantic_hashes) != membership_digests(catalog):
            raise KernelRefusal("CATALOG_MEMBERSHIP_MISMATCH")
        if not catalog.membership_matches_cardinality():
            raise KernelRefusal("CATALOG_CARDINALITY_MISMATCH")
        state = self._state()
        digest = content_hash(_plain(record))
        current = state.get("activeEpoch")
        if (
            current == record.epoch_id
            and state["epochs"][current].get("epochHash") == digest
            and (state.get("catalog") or {}).get("catalogHash") == catalog.catalog_hash
        ):
            for envelope in self._event_store.iterate():
                if envelope.idempotency_key == f"epoch:{record.epoch_id}:{digest}":
                    return envelope
            raise KernelRefusal("ACTIVE_EPOCH_EVENT_NOT_FOUND")
        if current is not None and safety_relevant:
            if self._event_store.uncertain_transactions():
                raise KernelRefusal("EPOCH_DRAIN_REQUIRED")
            if any(case.get("deployedSuccess") for case in state["cases"].values()):
                raise KernelRefusal("EPOCH_DRAIN_REQUIRED")
            live = [
                trial
                for trial in state["trials"].values()
                if trial["state"]
                not in {
                    TrialState.SETTLED_SUCCESS.value,
                    TrialState.SETTLED_NON_SUCCESS.value,
                    TrialState.INCIDENT_LOCKDOWN.value,
                }
            ]
            if live:
                raise KernelRefusal("EPOCH_DRAIN_REQUIRED")
        expected_catalog = _catalog_payload(catalog)
        staged = state.get("catalogs", {}).get(record.epoch_id)
        if staged is None:
            self._append(
                "CatalogFrozen",
                object_id=catalog.contract_id,
                now=now,
                payload=expected_catalog,
                idempotency_key=f"catalog:{record.epoch_id}:{catalog.catalog_hash}",
            )
        elif staged != expected_catalog:
            raise KernelRefusal("STAGED_CATALOG_COLLISION")
        return self._append(
            "EpochFrozen",
            object_id=record.epoch_id,
            now=now,
            payload=_epoch_payload(record, digest=digest),
            idempotency_key=f"epoch:{record.epoch_id}:{digest}",
        )

    def open_case(
        self,
        *,
        case_id: str,
        policy: CoordinationCasePolicy,
        active_vector: str,
        usable_reserve: Mapping[str, Mapping[str, Any]],
        reserve_per_trial: Mapping[str, Mapping[str, Any]],
        evidence_cells: Sequence[EvidenceCell],
        now: str,
        lazy_evidence_cells: bool = False,
    ) -> EventEnvelope:
        """Open a finite case under an epoch-frozen policy.

        ``lazy_evidence_cells``: the caller registers one cell per candidate of
        a frozen *domain* when that candidate is first trialled, instead of all
        of them up front (2026-09-19); every point not yet registered then
        counts as an open obligation.
        """
        state = self._state()
        if state.get("activeEpoch") is None:
            raise KernelRefusal("NO_ACTIVE_EPOCH")
        if case_id in state["cases"]:
            raise KernelRefusal("CASE_ALREADY_EXISTS")
        if policy.max_trials <= 0 or policy.max_proposals <= 0 or policy.deadline_ms <= 0:
            raise KernelRefusal("NON_FINITE_CASE_POLICY")
        active_epoch = state["epochs"][state["activeEpoch"]]
        frozen_harm_refs = set(active_epoch.get("harmContractHashes", {}))
        required_harm_refs = set(policy.harm_contract_refs)
        if not required_harm_refs or not required_harm_refs.issubset(frozen_harm_refs):
            raise KernelRefusal("HARM_CONTRACT_NOT_FROZEN")
        if set(usable_reserve) != required_harm_refs or set(
            reserve_per_trial
        ) != required_harm_refs:
            raise KernelRefusal("HARM_RESERVE_SET_MISMATCH")
        if active_vector not in set(active_epoch.get("targetVectorOrder", [])):
            raise KernelRefusal("TARGET_VECTOR_NOT_FROZEN")
        harm_kinds: Dict[str, str] = {}
        for harm_contract_ref in sorted(required_harm_refs):
            usable = usable_reserve[harm_contract_ref]
            per_trial = reserve_per_trial[harm_contract_ref]
            if (
                not isinstance(usable, Mapping)
                or not isinstance(per_trial, Mapping)
                or usable.get("unit") != per_trial.get("unit")
                or float(usable.get("value", 0)) <= 0
                or float(per_trial.get("value", 0)) <= 0
                or float(per_trial.get("value", 0)) > float(usable.get("value", 0))
            ):
                raise KernelRefusal("INVALID_TYPED_HARM_RESERVE")
            frozen_hash = active_epoch["harmContractHashes"][harm_contract_ref]
            frozen_record = state["contracts"].get(frozen_hash)
            if (
                frozen_record is None
                or frozen_record.get("family") != "HarmContract"
            ):
                raise KernelRefusal(
                    "HARM_CONTRACT_NOT_RESOLVED", detail=harm_contract_ref
                )
            frozen_body = frozen_record.get("body", {})
            frozen_kind = frozen_body.get(
                "harm_kind", frozen_body.get("harmKind", HarmKind.TRIAL_INDUCED.value)
            )
            try:
                harm_kinds[harm_contract_ref] = HarmKind(frozen_kind).value
            except ValueError as exc:
                raise KernelRefusal("INVALID_FROZEN_HARM_KIND") from exc
            frozen_reserve = frozen_body.get("reserve")
            if isinstance(frozen_reserve, Mapping) and (
                frozen_reserve.get("unit") != usable.get("unit")
                or float(frozen_reserve.get("value", 0))
                != float(usable.get("value", 0))
            ):
                raise KernelRefusal("HARM_RESERVE_DIFFERS_FROM_FROZEN_CONTRACT")
        deadline_at = format_utc(parse_utc(now) + timedelta(milliseconds=policy.deadline_ms))
        envelope = self._append(
            "CaseOpened",
            object_id=case_id,
            now=now,
            payload={
                "activeVector": active_vector,
                "caseId": case_id,
                "deadlineAt": deadline_at,
                "epochId": state["activeEpoch"],
                "harmContractRefs": list(policy.harm_contract_refs),
                "harmKinds": harm_kinds,
                "maxConsecutiveIndeterminate": policy.max_consecutive_indeterminate,
                "maxProposals": policy.max_proposals,
                "maxTrials": policy.max_trials,
                "policyId": policy.contract_id,
                "requireRecoveryBeforeNextTrial": policy.require_recovery_before_next_trial,
                "reservePerTrial": _plain(reserve_per_trial),
                "startedAt": now,
                "targetReleasePolicyRef": policy.target_release_policy_ref,
                "usableReserve": _plain(usable_reserve),
                **({"lazyEvidenceCells": True} if lazy_evidence_cells else {}),
            },
        )
        for evidence_cell in evidence_cells:
            self.register_evidence_cell(evidence_cell, case_id=case_id, now=now)
        return envelope

    def pause_case(self, case_id: str, *, paused: bool, now: str) -> EventEnvelope:
        """Pause only the scheduling of a subsequent trial."""
        if case_id not in self._state()["cases"]:
            raise KernelRefusal("UNKNOWN_CASE")
        return self._append(
            "CasePauseChanged",
            object_id=case_id,
            now=now,
            payload={"caseId": case_id, "paused": bool(paused)},
        )

    def register_evidence_cell(
        self, cell: EvidenceCell, *, case_id: str, now: str
    ) -> EventEnvelope:
        """Register one frozen evidence obligation in the active epoch."""
        # Only scalar admission facts are read before append; borrowing avoids
        # copying an ever-growing catalog/obligation map for each candidate.
        state = self._current_state()
        if case_id not in state["cases"]:
            raise KernelRefusal("UNKNOWN_CASE")
        if cell.required_independent_contributions <= 0:
            raise KernelRefusal("INVALID_EVIDENCE_QUOTA")
        epoch_id = str(state["activeEpoch"])
        return self._append(
            "EvidenceCellRegistered",
            object_id=cell.cell_id,
            now=now,
            payload=_cell_payload(
                cell, case_id=case_id, epoch_id=epoch_id
            ),
        )

    def record_compatibility(
        self, record: CompatibilityRecord, *, now: str
    ) -> EventEnvelope:
        """Record all nine deterministic cross-epoch compatibility checks."""
        required = {check.value for check in CompatibilityCheck}
        results = {str(key): bool(value) for key, value in record.results.items()}
        if set(results) != required:
            raise KernelRefusal("INCOMPLETE_COMPATIBILITY_CHECK")
        if record.admitted != all(results.values()):
            raise KernelRefusal("INCONSISTENT_COMPATIBILITY_RECORD")
        return self._append(
            "CompatibilityRecorded",
            object_id=record.record_id,
            now=now,
            payload={
                "admitted": record.admitted,
                "candidateSemanticHash": record.candidate_semantic_hash,
                "recordId": record.record_id,
                "refusalReason": record.refusal_reason,
                "results": results,
                "sourceEpochRef": record.source_epoch_ref,
                "targetEpochRef": record.target_epoch_ref,
            },
        )

    def record_frozen_contract(
        self, contract: Any, *, contract_hash: str, now: str
    ) -> EventEnvelope:
        """Attach an already-frozen contract body to a replayable epoch.

        The hash is checked over the complete dataclass.  This method does not
        perform admission; live admission goes through :meth:`admit_contract`.
        It exists so a replay can resolve an epoch's content-addressed objects
        without KCON re-running admission.
        """
        if not is_dataclass(contract):
            raise TypeError("frozen contract must be a dataclass")
        actual = content_hash(_plain(contract))
        if contract_hash != actual:
            raise KernelRefusal("CONTRACT_HASH_MISMATCH")
        contract_id = getattr(contract, "contract_id", type(contract).__name__)
        state = self._state()
        active_epoch = state.get("activeEpoch")
        if active_epoch is None:
            raise KernelRefusal("NO_ACTIVE_EPOCH")
        family = type(contract).__name__
        epoch_fields = {
            "TargetContract": "targetContractHashes",
            "MeasurementContract": "measurementContractHashes",
            "HarmContract": "harmContractHashes",
            "CapabilityManifest": "capabilityManifestHashes",
        }
        epoch_field = epoch_fields.get(family)
        frozen_hash = (
            state["epochs"][active_epoch].get(epoch_field, {}).get(str(contract_id))
            if epoch_field is not None
            else None
        )
        if frozen_hash != contract_hash:
            raise KernelRefusal(
                "CONTRACT_NOT_FROZEN_IN_EPOCH", detail=str(contract_id)
            )
        return self._append(
            "ContractAdmitted",
            object_id=str(contract_id),
            now=now,
            payload={
                "body": _plain(contract),
                "contractHash": contract_hash,
                "contractId": str(contract_id),
                "epochId": active_epoch,
                "family": family,
                "replayResolved": True,
            },
            idempotency_key=f"frozen-contract:{contract_hash}",
        )

    def _mailbox_state(
        self, envelope: MailboxEnvelope, expected_epoch_hash: Optional[str]
    ) -> EnvelopeAdmissionState:
        state = EnvelopeAdmissionState(
            supported_schema_versions=frozenset({ASSURANCE_SCHEMA_VERSION}),
            allowed_sources=ADVISORY_COMPONENTS,
            expected_epoch_hash=expected_epoch_hash,
        )
        for accepted in self._event_store.iterate():
            if accepted.event_kind != "AdvisoryAccepted":
                continue
            payload = accepted.payload
            if payload.get("mailboxObjectId") != envelope.object_id:
                continue
            state.last_sequence = int(payload["mailboxSequence"])
            state.seen_event_ids.add(str(payload["mailboxEventId"]))
            state.seen_idempotency[str(payload["mailboxIdempotencyKey"])] = str(
                payload["mailboxContentHash"]
            )
        return state

    def _record_advisory_rejection(
        self,
        envelope: MailboxEnvelope,
        *,
        reason: str,
        now: str,
    ) -> EventEnvelope:
        return self._append(
            "AdvisoryRejected",
            object_id=f"advisory:{envelope.object_id}",
            now=now,
            payload={
                "correlationId": envelope.correlation_id,
                "mailboxEventId": envelope.event_id,
                "messageKind": envelope.message_kind,
                "reason": reason,
            },
        )

    # ------------------------------------------------------------------ #
    # Advisory mailbox (design section 4.2, task section 3.2-3.4)
    # ------------------------------------------------------------------ #

    def submit_advisory(
        self, envelope: MailboxEnvelope, *, now: str
    ) -> Optional[EnvelopeRejection]:
        """Accept or reject one typed advisory message.

        Signature frozen; body owned by lane **KERN**.

        The *only* way an agent reaches the Kernel.  Returns ``None`` when the
        message is admitted into the mailbox, or the
        :class:`~assurance.core.envelopes.EnvelopeRejection` that refused it.

        Must apply :func:`assurance.core.envelopes.classify_envelope` against
        the current admission state -- which rejects hallucinated, stale,
        reordered, duplicate, expired, colliding and wrong-epoch messages
        (design section 15) -- and then, for an admitted message, verify that
        its typed content only *names* things the frozen epoch already
        contains.  A proposal referring to a candidate outside the frozen
        catalog is refused, not added: design section 6.3 forbids agents
        adding, removing or mutating candidates during an epoch.

        Admitting a message means only that the Kernel will consider it.  It
        confers nothing: the message cannot set a target, a threshold, a
        verdict, an evidence closure, a harm charge, a target release or a
        terminal state (task section 3.3).  Every one of those remains a
        Kernel decision made from contracts and raw evidence.
        """
        state = self._state()
        active_epoch_id = state.get("activeEpoch")
        expected_epoch_hash = None
        if active_epoch_id is not None:
            expected_epoch_hash = state["epochs"][active_epoch_id].get("epochHash")
        admission = self._mailbox_state(envelope, expected_epoch_hash)
        rejection = classify_envelope(envelope, admission, now=now)
        if rejection is not None:
            self._record_advisory_rejection(
                envelope, reason=rejection.value, now=now
            )
            return rejection

        payload = envelope.payload
        issued_by = payload.get("issuedBy")
        payload_epoch = payload.get("epochHash")
        correlation_id = payload.get("correlationId")
        if (
            issued_by not in {None, envelope.source_component.value}
            or payload_epoch not in {None, envelope.epoch_hash}
            or correlation_id not in {None, envelope.correlation_id}
        ):
            self._record_advisory_rejection(
                envelope, reason="ADVISORY_ENVELOPE_BODY_MISMATCH", now=now
            )
            return EnvelopeRejection.CONTENT_HASH_MISMATCH

        aliases = {
            "IntentDraft": "INTENT_DRAFT",
            "INTENT_DRAFT": "INTENT_DRAFT",
            "CandidateAssessment": "CANDIDATE_ASSESSMENT",
            "CANDIDATE_ASSESSMENT": "CANDIDATE_ASSESSMENT",
            "NextCandidateProposal": "NEXT_CANDIDATE_PROPOSAL",
            "NEXT_CANDIDATE_PROPOSAL": "NEXT_CANDIDATE_PROPOSAL",
        }
        expected_sources = {
            "INTENT_DRAFT": ComponentId.INTENT_AGENT.value,
            "CANDIDATE_ASSESSMENT": ComponentId.XAPP_AGENT.value,
            "NEXT_CANDIDATE_PROPOSAL": ComponentId.EVIDENCE_COORDINATOR.value,
        }
        body_fields = {
            "INTENT_DRAFT": {
                "objective_family",
                "objectiveFamily",
                "scope_selector",
                "scopeSelector",
                "proposed_constraints",
                "proposedConstraints",
                "unsupported_requests",
                "unsupportedRequests",
                "explanation",
            },
            "CANDIDATE_ASSESSMENT": {
                "candidate_id",
                "candidateId",
                "applicable",
                "expected_effect",
                "expectedEffect",
                "evidence_needs",
                "evidenceNeeds",
                "risk_notes",
                "riskNotes",
            },
            "NEXT_CANDIDATE_PROPOSAL": {
                "candidate_id",
                "candidateId",
                "evidence_cell_refs",
                "evidenceCellRefs",
                "expected_information_gain",
                "expectedInformationGain",
                "rationale",
            },
        }
        required_body_fields = {
            "INTENT_DRAFT": (
                {"objective_family", "objectiveFamily"},
                {"scope_selector", "scopeSelector"},
            ),
            "CANDIDATE_ASSESSMENT": (
                {"candidate_id", "candidateId"},
                {"applicable"},
            ),
            "NEXT_CANDIDATE_PROPOSAL": (
                {"candidate_id", "candidateId"},
            ),
        }
        outer_fields = {
            "messageId",
            "kind",
            "issuedBy",
            "correlationId",
            "epochHash",
            "createdAt",
            "body",
        }
        envelope_kind = aliases.get(envelope.message_kind)
        payload_kind = aliases.get(str(payload.get("kind")))
        body = payload.get("body")
        typed = (
            set(payload) == outer_fields
            and envelope_kind is not None
            and payload_kind == envelope_kind
            and issued_by == expected_sources.get(envelope_kind)
            and issued_by == envelope.source_component.value
            and payload_epoch == envelope.epoch_hash
            and correlation_id == envelope.correlation_id
            and isinstance(payload.get("messageId"), str)
            and bool(payload.get("messageId"))
            and isinstance(body, Mapping)
            and set(body).issubset(body_fields.get(envelope_kind, set()))
            and all(
                set(body) & alternatives
                for alternatives in required_body_fields.get(envelope_kind, ())
            )
        )
        if typed:
            try:
                parse_utc(str(payload.get("createdAt")))
            except (TypeError, ValueError):
                typed = False
        if typed:
            assert isinstance(body, Mapping)
            def body_value(snake: str, camel: str, default: Any = None) -> Any:
                return body.get(snake, body.get(camel, default))

            if envelope_kind == "NEXT_CANDIDATE_PROPOSAL":
                evidence_refs = body_value(
                    "evidence_cell_refs", "evidenceCellRefs", []
                )
                typed = (
                    isinstance(body_value("candidate_id", "candidateId"), str)
                    and isinstance(evidence_refs, (list, tuple))
                    and all(isinstance(item, str) for item in evidence_refs)
                    and set(evidence_refs).issubset(state["evidenceCells"])
                    and isinstance(body.get("rationale", ""), str)
                )
            elif envelope_kind == "CANDIDATE_ASSESSMENT":
                evidence_needs = body_value("evidence_needs", "evidenceNeeds", [])
                typed = (
                    isinstance(body_value("candidate_id", "candidateId"), str)
                    and isinstance(body.get("applicable"), bool)
                    and isinstance(evidence_needs, (list, tuple))
                    and all(isinstance(item, str) for item in evidence_needs)
                    and set(evidence_needs).issubset(state["evidenceCells"])
                    and isinstance(body_value("risk_notes", "riskNotes", ""), str)
                )
            elif envelope_kind == "INTENT_DRAFT":
                scope = body_value("scope_selector", "scopeSelector")
                unsupported = body_value(
                    "unsupported_requests", "unsupportedRequests", []
                )
                typed = (
                    isinstance(body_value("objective_family", "objectiveFamily"), str)
                    and isinstance(scope, Mapping)
                    and all(
                        isinstance(key, str) and isinstance(value, str)
                        for key, value in scope.items()
                    )
                    and isinstance(unsupported, (list, tuple))
                    and all(isinstance(item, str) for item in unsupported)
                    and isinstance(body.get("explanation", ""), str)
                )
        if not typed:
            self._record_advisory_rejection(
                envelope, reason="MALFORMED_TYPED_ADVISORY", now=now
            )
            return EnvelopeRejection.SOURCE_NOT_PERMITTED

        forbidden = {
            "actuator_command",
            "authorization",
            "case_termination",
            "catalog_membership",
            "charge",
            "evidence_closure",
            "harm_charge",
            "ledger_update",
            "target_release",
            "terminal_state",
            "threshold_override",
            "token",
            "verdict",
        }

        def keys_below(value: Any) -> set[str]:
            if isinstance(value, Mapping):
                result = {str(key) for key in value}
                for item in value.values():
                    result.update(keys_below(item))
                return result
            if isinstance(value, (list, tuple)):
                result: set[str] = set()
                for item in value:
                    result.update(keys_below(item))
                return result
            return set()

        if keys_below(payload) & forbidden:
            self._record_advisory_rejection(
                envelope, reason="ADVISORY_ATTEMPTED_KERNEL_MUTATION", now=now
            )
            return EnvelopeRejection.SOURCE_NOT_PERMITTED

        candidate_id = None
        if isinstance(body, Mapping):
            candidate_id = body.get("candidateId", body.get("candidate_id"))
        if candidate_id is None:
            candidate_id = payload.get("candidateId")
        candidate_kinds = {
            "CandidateAssessment",
            "NextCandidateProposal",
            "CANDIDATE_ASSESSMENT",
            "NEXT_CANDIDATE_PROPOSAL",
        }
        if envelope.message_kind in candidate_kinds:
            catalog = state.get("catalog") or {}
            if frozen_candidate(catalog, candidate_id) is None:
                self._record_advisory_rejection(
                    envelope, reason="CANDIDATE_NOT_FROZEN", now=now
                )
                return EnvelopeRejection.EPOCH_MISMATCH

        self._append(
            "AdvisoryAccepted",
            object_id=f"advisory:{envelope.object_id}",
            now=now,
            payload={
                "candidateId": candidate_id,
                "correlationId": envelope.correlation_id,
                "mailboxContentHash": envelope.content_hash,
                "mailboxEventId": envelope.event_id,
                "mailboxIdempotencyKey": envelope.idempotency_key,
                "mailboxObjectId": envelope.object_id,
                "mailboxSequence": envelope.sequence,
                "messageKind": envelope.message_kind,
            },
        )
        return None

    # ------------------------------------------------------------------ #
    # Admission and epoch (design sections 6.2, 6.3)
    # ------------------------------------------------------------------ #

    def admit_contract(
        self, contract: Any, *, confirmation: Optional[ConfirmationRecord], now: str
    ) -> EventEnvelope:
        """Admit one contract into the pending epoch.

        Signature frozen; body owned by lane **KERN**.

        Validates through
        :func:`assurance.contracts.validation.validate_contract`, then appends
        an admission event carrying the contract's content hash.

        *confirmation* is required for any contract the Operator must confirm
        -- the target vector and the case policy -- and must still cover the
        contract's current content hash
        (:meth:`~assurance.core.confirmation.ConfirmationRecord.is_valid_for`).
        Design section 5: if confirmed content changes, the previous
        confirmation is invalid and a new click is required.  Admitting under
        a stale confirmation would make the Operator's one control decorative.

        Raises :class:`KernelRefusal` on validation failure.  There is no
        partial admission.
        """
        self._ensure_initialized()
        try:
            validate_contract(contract)
            digest = contract_content_hash(contract)
            body = canonical_form(contract)
        except Exception as exc:
            raise KernelRefusal("CONTRACT_ADMISSION_FAILED", detail=str(exc)) from exc
        if isinstance(contract, (TargetVector, CoordinationCasePolicy)):
            if confirmation is None or not confirmation.is_valid_for(digest):
                raise KernelRefusal("CONFIRMATION_REQUIRED_OR_STALE")
        contract_id = getattr(contract, "contract_id", type(contract).__name__)
        envelope = self._append(
            "ContractAdmitted",
            object_id=str(contract_id),
            now=now,
            payload={
                "body": body,
                "confirmationRef": (
                    confirmation.event_id if confirmation is not None else None
                ),
                "contractHash": digest,
                "contractId": str(contract_id),
                "family": type(contract).__name__,
                "replayResolved": False,
            },
            idempotency_key=f"contract:{digest}",
        )
        self._pending_contract_objects[digest] = contract
        return envelope

    def admit_deployment(self, binding: Any, *, now: str) -> EventEnvelope:
        """Admit one deployment binding.

        Signature frozen; body owned by lane **KERN**.

        Must call
        :func:`assurance.contracts.validation.assert_secret_free` before
        appending anything, because the admission event itself becomes part of
        the exported evidence bundle (design section 17.10).
        """
        self._ensure_initialized()
        try:
            assert_secret_free(binding)
            validate_contract(binding)
            digest = contract_content_hash(binding)
            body = canonical_form(binding)
        except Exception as exc:
            raise KernelRefusal("DEPLOYMENT_ADMISSION_FAILED", detail=str(exc)) from exc
        binding_id = getattr(binding, "contract_id", type(binding).__name__)
        envelope = self._append(
            "DeploymentAdmitted",
            object_id=str(binding_id),
            now=now,
            payload={
                "bindingHash": digest,
                "bindingId": str(binding_id),
                "body": body,
            },
            idempotency_key=f"deployment:{digest}",
        )
        self._pending_deployment_objects[digest] = binding
        return envelope

    def freeze_epoch(self, *, confirmation: ConfirmationRecord, now: str) -> EpochRecord:
        """Freeze the admitted contract set into an evidence epoch.

        Signature frozen; body owned by lane **KERN**.

        Composes :func:`assurance.contracts.validation.validate_family_set`,
        :func:`assurance.contracts.catalog.generate_catalog` and
        :func:`assurance.contracts.epoch.freeze_epoch`, appends the epoch
        event, and returns the record.

        After this call the candidate universe, the contract meanings, the
        bindings, the target-vector order, the case limits and the
        evaluator/reducer versions are fixed.  A later change produces a new
        epoch; task section 6.14 additionally requires a safety-relevant
        change to drain in-flight transactions and resolve the live deployment
        before the new epoch activates, so this method must refuse to freeze
        while a trial is post-commit.
        """
        self._ensure_initialized()
        if self._event_store.uncertain_transactions():
            raise KernelRefusal("EPOCH_DRAIN_REQUIRED")
        state = self._state()
        if state.get("activeEpoch") is not None and any(
            case.get("deployedSuccess") for case in state["cases"].values()
        ):
            raise KernelRefusal("EPOCH_DRAIN_REQUIRED")
        live = [
            trial
            for trial in state["trials"].values()
            if trial["state"]
            not in {
                TrialState.SETTLED_SUCCESS.value,
                TrialState.SETTLED_NON_SUCCESS.value,
                TrialState.INCIDENT_LOCKDOWN.value,
            }
        ]
        if live:
            raise KernelRefusal("EPOCH_DRAIN_REQUIRED")

        from assurance.contracts.capability import (
            ActuatorBinding,
            CapabilityManifest,
            CompositionManifest,
            DeploymentBinding,
        )
        from assurance.contracts.catalog import CoordinationCasePolicy
        from assurance.contracts.harm import HarmContract
        from assurance.contracts.measurement import CounterBinding, MeasurementContract
        from assurance.contracts.target import TargetContract, TargetVector

        contracts = list(self._pending_contract_objects.values())
        deployments = list(self._pending_deployment_objects.values())
        if not contracts:
            raise KernelRefusal("NO_PENDING_CONTRACTS")
        try:
            validate_family_set([*contracts, *deployments])
            targets = [item for item in contracts if isinstance(item, TargetContract)]
            harms = [item for item in contracts if isinstance(item, HarmContract)]
            measurements = [
                item for item in contracts if isinstance(item, MeasurementContract)
            ]
            capabilities = [
                item for item in contracts if isinstance(item, CapabilityManifest)
            ]
            composition = next(
                item for item in contracts if isinstance(item, CompositionManifest)
            )
            target_vector = next(
                item for item in contracts if isinstance(item, TargetVector)
            )
            case_policy = next(
                item for item in contracts if isinstance(item, CoordinationCasePolicy)
            )
            counter_bindings = [
                item for item in contracts if isinstance(item, CounterBinding)
            ]
            actuator_bindings = [
                item for item in contracts if isinstance(item, ActuatorBinding)
            ]
            deployment_bindings = [
                item for item in deployments if isinstance(item, DeploymentBinding)
            ]
            seed = content_hash(
                {
                    "confirmation": confirmation.event_id,
                    "contracts": sorted(self._pending_contract_objects),
                    "deployments": sorted(self._pending_deployment_objects),
                    "reducerVersion": self._reducer.reducer_version,
                }
            )
            epoch_id = f"epoch-{seed[:16]}"
            catalog = generate_catalog(
                targets=targets,
                capabilities=capabilities,
                composition=composition,
                generator_version="assurance-catalog/1.0.0",
                epoch_ref=epoch_id,
                identity={
                    "contract_id": f"catalog/{epoch_id}",
                    "version": "1.0.0",
                    "schema_version": ASSURANCE_SCHEMA_VERSION,
                    "document_status": "NORMATIVE",
                    "standard_mapping": {},
                },
            )
            record = build_epoch(
                identity={
                    "contract_id": f"epoch/{epoch_id}",
                    "version": "1.0.0",
                    "schema_version": ASSURANCE_SCHEMA_VERSION,
                    "document_status": "NORMATIVE",
                    "standard_mapping": {},
                },
                epoch_id=epoch_id,
                frozen_at=now,
                targets=targets,
                harms=harms,
                measurements=measurements,
                capabilities=capabilities,
                composition=composition,
                target_vector=target_vector,
                case_policy=case_policy,
                deployment_bindings=deployment_bindings,
                counter_bindings=counter_bindings,
                actuator_bindings=actuator_bindings,
                catalog=catalog,
                evaluator_version="assurance-evaluator/1.0.0",
                reducer_version=self._reducer.reducer_version,
                confirmation=confirmation,
                supersedes_epoch_ref=self._state().get("activeEpoch"),
            )
            digest = epoch_hash(record)
        except Exception as exc:
            if isinstance(exc, KernelRefusal):
                raise
            raise KernelRefusal("EPOCH_FREEZE_FAILED", detail=str(exc)) from exc
        self._append(
            "CatalogFrozen",
            object_id=catalog.contract_id,
            now=now,
            payload=_catalog_payload(catalog),
            idempotency_key=f"catalog:{record.epoch_id}:{catalog.catalog_hash}",
        )
        self._append(
            "EpochFrozen",
            object_id=record.epoch_id,
            now=now,
            payload=_epoch_payload(record, digest=digest),
            idempotency_key=f"epoch:{record.epoch_id}:{digest}",
        )
        return record

    def current_catalog(self) -> CandidateCatalog:
        """The frozen catalog of the active epoch.

        Signature frozen; body owned by lane **KERN**.

        Must verify
        :meth:`~assurance.contracts.catalog.CandidateCatalog.membership_matches_cardinality`
        and the recorded catalog hash before returning: a catalog that no
        longer matches its epoch is a corruption, and serving it would let a
        trial run against a universe nobody froze.
        """
        state = self._state()
        payload = state.get("catalog")
        active_epoch = state.get("activeEpoch")
        if payload is None or active_epoch is None:
            raise KernelRefusal("NO_ACTIVE_CATALOG")
        epoch = state["epochs"][active_epoch]
        if payload.get("catalogHash") != epoch.get("catalogHash"):
            raise KernelRefusal("CATALOG_EPOCH_MISMATCH")
        candidates = (DomainMembership(payload["domain"]) if payload.get("domain") else tuple(
            Candidate(
                candidate_id=item["candidateId"],
                target_ref=item["targetRef"],
                option_ref=item["optionRef"],
                parameters=item.get("parameters", {}),
                semantic_hash=item["semanticHash"],
                capability_ref=item["capabilityRef"],
            )
            for item in payload.get("candidates", [])
        ))
        catalog = CandidateCatalog(
            contract_id=payload.get("contractId", f"catalog/{active_epoch}"),
            version=payload.get("version", "1.0.0"),
            schema_version=payload.get("schemaVersion", ASSURANCE_SCHEMA_VERSION),
            document_status=payload.get("documentStatus", "NORMATIVE"),
            standard_mapping=payload.get("standardMapping", {}),
            generator_version=payload["generatorVersion"],
            cardinality=int(payload["cardinality"]),
            candidates=candidates,
            catalog_hash=payload["catalogHash"],
            epoch_ref=payload.get("epochRef", active_epoch),
        )
        if not catalog.membership_matches_cardinality():
            raise KernelRefusal("CATALOG_CARDINALITY_MISMATCH")
        return catalog

    # ------------------------------------------------------------------ #
    # Trial lifecycle (design section 7, task section 6)
    # ------------------------------------------------------------------ #

    def open_trial(self, *, candidate_id: str, case_id: str, now: str) -> str:
        """Open a trial for a frozen catalog candidate; return its trial id.

        Signature frozen; body owned by lane **KERN**.

        The candidate must be in the current frozen catalog and
        ``AVAILABLE`` on the candidate-availability axis.  Must refuse while
        any uncertain transaction remains unresolved
        (:meth:`~assurance.kernel.event_store.EventStore.uncertain_transactions`)
        -- design section 8 blocks new trials until recovery completes -- and
        while the case policy's deadline, trial cap or proposal cap is spent.
        """
        state = self._state()
        try:
            case = state["cases"][case_id]
        except KeyError as exc:
            raise KernelRefusal("UNKNOWN_CASE", detail=case_id) from exc
        if case.get("terminal") is not None:
            raise KernelRefusal("CASE_TERMINAL", detail=str(case["terminal"]))
        if case.get("paused"):
            raise KernelRefusal("CASE_PAUSED")
        if any(
            trial.get("caseId") == case_id
            and trial.get("state") == TrialState.INCIDENT_LOCKDOWN.value
            for trial in state["trials"].values()
        ):
            raise KernelRefusal("INCIDENT_LOCKDOWN")
        if parse_utc(now) >= parse_utc(case["deadlineAt"]):
            raise KernelRefusal("CASE_DEADLINE_REACHED")
        if self._event_store.uncertain_transactions():
            raise KernelRefusal(
                "RECOVERY_BLOCKED",
                detail=",".join(self._event_store.uncertain_transactions()),
            )
        if any(
            trial.get("caseId") == case_id
            and trial.get("state")
            not in {
                TrialState.SETTLED_SUCCESS.value,
                TrialState.SETTLED_NON_SUCCESS.value,
                TrialState.INCIDENT_LOCKDOWN.value,
            }
            for trial in state["trials"].values()
        ):
            raise KernelRefusal("ACTIVE_TRIAL_EXISTS")
        case_trials = [
            trial for trial in state["trials"].values() if trial["caseId"] == case_id
        ]
        if len(case_trials) >= int(case["maxTrials"]):
            raise KernelRefusal("TRIAL_CAP_REACHED")
        proposals = 0
        for envelope in self._event_store.iterate():
            if envelope.event_kind not in {"AdvisoryAccepted", "AdvisoryRejected"}:
                continue
            if envelope.payload.get("correlationId") != case_id:
                continue
            if envelope.payload.get("messageKind") in {
                "NextCandidateProposal",
                "NEXT_CANDIDATE_PROPOSAL",
            }:
                proposals += 1
        if proposals >= int(case["maxProposals"]):
            raise KernelRefusal("PROPOSAL_CAP_REACHED")
        catalog = state.get("catalog") or {}
        candidate = frozen_candidate(catalog, candidate_id)
        if candidate is None:
            raise KernelRefusal("CANDIDATE_NOT_FROZEN", detail=candidate_id)
        if candidate_availability(state, candidate_id) != CandidateAvailability.AVAILABLE.value:
            raise KernelRefusal("CANDIDATE_NOT_AVAILABLE", detail=candidate_id)
        remaining = self._remaining_reserve(case_id, state=state)
        if remaining is not None and remaining <= 0:
            raise KernelRefusal("USABLE_RESERVE_EXHAUSTED")
        index = len(case_trials) + 1
        trial_id = f"{case_id}:trial:{index}"
        transaction_id = f"tx:{trial_id}"
        self._append(
            "TrialOpened",
            object_id=trial_id,
            now=now,
            payload={
                "candidateId": candidate_id,
                "candidateSemanticHash": candidate["semanticHash"],
                "caseId": case_id,
                "epochId": state["activeEpoch"],
                "resourceId": candidate["capabilityRef"],
                "targetRef": candidate["targetRef"],
                "transactionId": transaction_id,
                "trialId": trial_id,
            },
        )
        return trial_id

    def stage_actuation_plan(
        self, trial_id: str, *, plan: Mapping[str, Any], now: str
    ) -> EventEnvelope:
        """Bind one actuation plan to a trial before any permit is issued.

        Added at Gate 2 (`docs/architecture/SEAMS-GATE2.md`).  Without it the
        Kernel has no configuration digest to put on a
        :class:`~assurance.gateway.token.KernelToken`, whose
        ``expected_config_hash`` is defined as "what the gateway must observe
        *before* acting" -- so every gateway operation would be answered
        ``REJECTED_CONFIG_MISMATCH`` and the vertical path could not run at
        all.

        Three things are checked here and nowhere else:

        * the plan parses as an
          :class:`~assurance.gateway.plan.ActuationPlan`, so a malformed plan
          never becomes a staged transaction;
        * its steps realise **exactly** the frozen catalog candidate's
          parameters.  An advisory names a candidate; the candidate fixes the
          parameters; the plan may not add, drop or retune an axis.  That is
          GAP-01 closed on the Kernel side as well as the gateway's;
        * it arms exactly the watchdogs the case's epoch-frozen harm contracts
          require, so the arming evidence the gateway reports back at ``READY``
          is the set :meth:`record_commit_readiness` expects.

        Refused once a permit exists for the transaction: re-staging under a
        live permit would move the configuration the permit already names.
        """
        self._ensure_initialized()
        from assurance.gateway.plan import ActuationPlan, PlanError

        state = self._state()
        try:
            trial = state["trials"][trial_id]
        except KeyError as exc:
            raise KernelRefusal("UNKNOWN_TRIAL", detail=trial_id) from exc
        if TrialState(trial["state"]) not in {
            TrialState.PROPOSED,
            TrialState.VALIDATING,
            TrialState.RESERVED,
            TrialState.PREPARING,
        }:
            raise KernelRefusal("PLAN_STAGED_AFTER_PREPARE", detail=trial["state"])
        if any(
            envelope.event_kind == "TokenIssued"
            and envelope.payload.get("transactionId") == trial["transactionId"]
            for envelope in self._event_store.iterate()
        ):
            raise KernelRefusal("PLAN_STAGED_UNDER_LIVE_PERMIT")
        try:
            staged = ActuationPlan.from_mapping(plan)
        except PlanError as exc:
            raise KernelRefusal("PLAN_NOT_ADMISSIBLE", detail=str(exc)) from exc

        catalog = state.get("catalog") or {}
        candidate = frozen_candidate(catalog, trial["candidateId"])
        if candidate is None:
            raise KernelRefusal("CANDIDATE_NOT_FROZEN", detail=trial["candidateId"])
        realised = {step.axis: str(step.value) for step in staged.steps}
        frozen_parameters = {
            str(key): str(value)
            for key, value in dict(candidate.get("parameters", {})).items()
        }
        if realised != frozen_parameters:
            raise KernelRefusal(
                "PLAN_DOES_NOT_REALISE_CANDIDATE",
                detail=f"{sorted(realised.items())} != {sorted(frozen_parameters.items())}",
            )
        expected_watchdogs = self._expected_watchdog_ids(trial, state=state)
        if tuple(staged.watchdogs) != expected_watchdogs:
            raise KernelRefusal(
                "PLAN_WATCHDOG_SET_MISMATCH",
                detail=f"{list(staged.watchdogs)} != {list(expected_watchdogs)}",
            )
        return self._append(
            "TrialPlanStaged",
            object_id=trial_id,
            now=now,
            payload={
                "adapter": staged.adapter,
                "appliedConfigHash": staged.applied_hash,
                "axes": list(staged.axes),
                "baselineConfigHash": staged.baseline_hash,
                "plan": staged.to_canonical_dict(),
                "planHash": staged.content_hash(),
                "prefixConfigHashes": list(staged.prefix_hashes()),
                "transactionId": trial["transactionId"],
                "trialId": trial_id,
                "watchdogIds": list(staged.watchdogs),
            },
            idempotency_key=f"plan:{trial_id}:{staged.content_hash()}",
        )

    def _expected_watchdog_ids(
        self, trial: Mapping[str, Any], *, state: Mapping[str, Any]
    ) -> Tuple[str, ...]:
        """Every watchdog the case's epoch-frozen harm contracts require.

        The same derivation :meth:`record_commit_readiness` checks the
        gateway's arming evidence against, so a plan cannot be staged that
        would be unable to reach ``COMMIT_DECIDED``.
        """
        case = state["cases"][trial["caseId"]]
        epoch = state["epochs"].get(trial["epochId"], {})
        identifiers: list[str] = []
        for contract_ref in sorted(case.get("harmContractRefs", [])):
            contract_hash = epoch.get("harmContractHashes", {}).get(contract_ref)
            record = state["contracts"].get(contract_hash)
            if record is None or record.get("family") != "HarmContract":
                raise KernelRefusal(
                    "WATCHDOG_CONTRACT_NOT_RESOLVED", detail=contract_ref
                )
            for watchdog in record.get("body", {}).get("watchdogs", []):
                identifier = watchdog.get(
                    "watchdog_id", watchdog.get("watchdogId")
                )
                if not identifier:
                    raise KernelRefusal(
                        "WATCHDOG_CONTRACT_NOT_ARMABLE", detail=contract_ref
                    )
                identifiers.append(str(identifier))
        return tuple(sorted(identifiers))

    def advance_trial(
        self,
        trial_id: str,
        target_state: TrialState,
        *,
        now: str,
        reason: Optional[StopReason] = None,
    ) -> TrialState:
        """Move a trial to *target_state*, appending the transition event.

        Signature frozen; body owned by lane **KERN**.

        Must validate against
        :data:`assurance.core.states.TRIAL_TRANSITIONS` via
        :func:`assurance.core.states.assert_transition` -- design acceptance
        criterion 17.3 requires every illegal transition to be rejected -- and
        must enforce the phase obligations the table alone cannot express:

        * nothing that changes real configuration before ``READY``
          (task section 6.2);
        * ``APPLYING`` only after every required watchdog is armed, the
          measurement baseline is frozen, and ``COMMIT_DECIDED`` is durable
          (task section 6.3);
        * the trial count and harm clock start exactly once, at the first
          real state-changing apply (task section 6.4);
        * guards stay armed from apply until recovery verification or live
          finalization (task section 6.5);
        * ``FINALIZING_LIVE`` only after all mandatory predicates passed in
          the same live trial and validity region through the full hold
          (design section 7).

        *reason* is required for any transition into ``STOPPING`` and is
        recorded at its :data:`assurance.core.states.SAFETY_PRECEDENCE`
        rank, so a harm breach is never filed as a KPI failure.
        """
        state = self._state()
        try:
            trial = state["trials"][trial_id]
            source = TrialState(trial["state"])
        except KeyError as exc:
            raise KernelRefusal("UNKNOWN_TRIAL", detail=trial_id) from exc
        if not isinstance(target_state, TrialState):
            raise KernelRefusal("UNKNOWN_TRIAL_STATE")
        try:
            assert_transition(source, target_state)
        except (ValueError, KeyError) as exc:
            raise KernelRefusal(
                "ILLEGAL_TRANSITION", detail=f"{source.value}->{target_state.value}"
            ) from exc

        if target_state is TrialState.RESERVED:
            reserved = any(
                entry.get("trialId") == trial_id
                and entry.get("movementKind") == "RESERVE"
                for entry in state["harmLedger"]
            )
            if not reserved or state["resourceLocks"].get(trial["resourceId"]) != trial_id:
                raise KernelRefusal("HARM_NOT_RESERVED")
        if target_state is TrialState.READY and not trial.get(
            "gatewayReadyAcknowledged"
        ):
            raise KernelRefusal("COMPONENTS_NOT_READY")
        if target_state is TrialState.COMMIT_DECIDED:
            expected_guard_refs = sorted(
                state["cases"][trial["caseId"]].get("harmContractRefs", [])
            )
            if (
                not trial.get("guardsArmed")
                or not trial.get("baselineHash")
                or trial.get("armedHarmContractRefs") != expected_guard_refs
            ):
                raise KernelRefusal("COMMIT_NOT_READY")
        if target_state is TrialState.APPLYING:
            if source is not TrialState.COMMIT_DECIDED:
                raise KernelRefusal("APPLY_BEFORE_DURABLE_COMMIT")
            if not trial.get("guardsArmed") or not trial.get("baselineHash"):
                raise KernelRefusal("APPLY_GUARDS_NOT_ARMED")
            if not trial.get("commitAcknowledged"):
                raise KernelRefusal("COMMIT_RESULT_NOT_ACKNOWLEDGED")
        if target_state is TrialState.STOPPING:
            if reason is None:
                raise KernelRefusal("STOP_REASON_REQUIRED")
            previous = trial.get("stopReason")
            if previous is not None:
                reason = strongest_reason((StopReason(previous), reason))
        elif reason is not None:
            raise KernelRefusal("STOP_REASON_OUTSIDE_STOPPING")
        if target_state is TrialState.FINALIZING_LIVE:
            evaluation = trial["evaluation"]
            mandatory_ids = tuple(evaluation.get("mandatoryPredicateIds", []))
            if (
                evaluation["executionValidity"] != ExecutionValidity.VALID.value
                or evaluation["measurementSufficiency"]
                != MeasurementSufficiency.SUFFICIENT.value
                or not evaluation["predicateVerdicts"]
                or any(
                    evaluation["predicateVerdicts"].get(predicate_id)
                    != PredicateVerdict.PASS.value
                    for predicate_id in mandatory_ids
                )
                or not evaluation.get("holdComplete")
                or not evaluation.get("sameCandidate")
                or not evaluation.get("validityRegionStable")
                or trial.get("stopReason") is not None
            ):
                raise KernelRefusal("SUCCESS_CONDITIONS_INCOMPLETE")
        if target_state is TrialState.SETTLEMENT:
            if source is TrialState.FINALIZING_LIVE and (
                not trial.get("configurationReread")
                or not trial.get("finalizeAcknowledged")
            ):
                raise KernelRefusal("LIVE_FINALIZE_INCOMPLETE")
            if source is TrialState.RECOVERY_VERIFYING and not trial.get(
                "recoveryVerified"
            ):
                raise KernelRefusal("RECOVERY_NOT_VERIFIED")

        self._append(
            "TrialStateChanged",
            object_id=trial_id,
            now=now,
            payload={
                "from": source.value,
                "reason": reason.value if reason is not None else None,
                "to": target_state.value,
                "transactionId": trial["transactionId"],
                "trialId": trial_id,
            },
        )
        return target_state

    def record_commit_readiness(
        self,
        trial_id: str,
        *,
        watchdogs_armed: bool,
        baseline_hash: str,
        now: str,
    ) -> EventEnvelope:
        """Freeze baseline and watchdog readiness before durable commit."""
        state = self._state()
        try:
            trial = state["trials"][trial_id]
        except KeyError as exc:
            raise KernelRefusal("UNKNOWN_TRIAL", detail=trial_id) from exc
        if trial["state"] != TrialState.READY.value:
            raise KernelRefusal("READINESS_OUTSIDE_READY")
        if not watchdogs_armed:
            raise KernelRefusal("WATCHDOGS_NOT_ARMED")
        if not is_content_hash(baseline_hash):
            raise KernelRefusal("INVALID_BASELINE_HASH")
        if trial.get("readyBaselineHash") != baseline_hash:
            raise KernelRefusal("BASELINE_NOT_GATEWAY_OBSERVED")
        case = state["cases"][trial["caseId"]]
        armed_contract_refs = sorted(case.get("harmContractRefs", []))
        if not armed_contract_refs:
            raise KernelRefusal("WATCHDOG_CONTRACTS_NOT_FROZEN")
        epoch = state["epochs"].get(trial["epochId"], {})
        expected_arming_evidence: set[str] = set()
        for contract_ref in armed_contract_refs:
            contract_hash = epoch.get("harmContractHashes", {}).get(contract_ref)
            contract_record = state["contracts"].get(contract_hash)
            if (
                contract_record is None
                or contract_record.get("family") != "HarmContract"
            ):
                raise KernelRefusal(
                    "WATCHDOG_CONTRACT_NOT_RESOLVED", detail=contract_ref
                )
            watchdogs = contract_record.get("body", {}).get("watchdogs", [])
            if not watchdogs:
                raise KernelRefusal(
                    "WATCHDOG_CONTRACTS_NOT_FROZEN", detail=contract_ref
                )
            for watchdog in watchdogs:
                watchdog_id = watchdog.get(
                    "watchdog_id", watchdog.get("watchdogId")
                )
                arm_before_apply = watchdog.get(
                    "arm_before_apply", watchdog.get("armBeforeApply", False)
                )
                if not watchdog_id or not arm_before_apply:
                    raise KernelRefusal(
                        "WATCHDOG_CONTRACT_NOT_ARMABLE", detail=contract_ref
                    )
                expected_arming_evidence.add(
                    f"watchdog:{watchdog_id}:armed"
                )
        observed_arming_evidence = set(trial.get("readyEvidenceRefs", []))
        missing = expected_arming_evidence - observed_arming_evidence
        kernel_hosted_guards: Tuple[str, ...] = ()
        if missing:
            # The deployment did not arm them.  That is admissible only when
            # the gateway reported that this adapter *cannot* host a watchdog,
            # in which case the Kernel arms the contract watchdog on its own
            # two mechanisms instead (SEAMS-GATE2.md section 8.3, judgement 2).
            # Silence is not the same statement and is refused.
            declared_kernel_hosted = any(
                str(ref).startswith(f"{WATCHDOG_HOSTING_PREFIX}:")
                and str(ref).endswith(":kernel")
                for ref in observed_arming_evidence
            )
            if not declared_kernel_hosted:
                raise KernelRefusal("WATCHDOG_ARMING_NOT_OBSERVED")
            kernel_hosted_guards = self._arm_kernel_hosted_watchdogs(
                trial, state=state, now=now
            )
        return self._append(
            "TrialCommitReadinessRecorded",
            object_id=trial_id,
            now=now,
            payload={
                "baselineHash": baseline_hash,
                "armedHarmContractRefs": armed_contract_refs,
                "armedWatchdogEvidenceRefs": sorted(expected_arming_evidence),
                "kernelHostedGuards": list(kernel_hosted_guards),
                "kernelHostedWatchdogIds": sorted(
                    ref.split(":")[1] for ref in missing
                ),
                "trialId": trial_id,
                "watchdogsArmed": True,
            },
            idempotency_key=f"readiness:{trial_id}:{baseline_hash}",
        )

    def _arm_kernel_hosted_watchdogs(
        self, trial: Mapping[str, Any], *, state: Mapping[str, Any], now: str
    ) -> Tuple[str, ...]:
        """Arm the two guards the Kernel hosts itself, or refuse.

        Judgement 2 of ``docs/architecture/SEAMS-GATE2.md`` section 8.3: an
        adapter whose transport cannot carry a contract watchdog is armed by
        the Kernel instead, on the two mechanisms it already owns end to end:

        ``measurement-staleness``
            The evaluator reports
            :attr:`~assurance.core.axes.MeasurementSufficiency.STALE` and
            :meth:`decide_trial` turns it into
            :attr:`~assurance.core.states.StopReason.TELEMETRY_STALE`.  Usable
            only if every frozen measurement contract states a positive
            freshness bound and a clock requirement -- a contract that does
            not is a guard with no threshold.

        ``token-lease-deadline``
            The permit's lease is the action deadline.  A result arriving
            after it is refused (``LEASE_EXPIRED``) and stops the trial, and
            the gateway's own watchdog drives the deployment to its contracted
            safe state.  Usable only if this transaction really holds a permit
            whose lease is still ahead of *now*.

        Both are *checked* here, not asserted: arming a guard the deployment's
        contracts cannot support would be the promise-instead-of-observation
        the whole rule exists to refuse.
        """
        epoch = state["epochs"].get(trial["epochId"], {})
        measurement_hashes = epoch.get("measurementContractHashes", {})
        if not measurement_hashes:
            raise KernelRefusal("KERNEL_WATCHDOG_NO_MEASUREMENT_CONTRACT")
        for identifier, contract_hash in sorted(measurement_hashes.items()):
            record = state["contracts"].get(contract_hash)
            if record is None or record.get("family") != "MeasurementContract":
                raise KernelRefusal(
                    "MEASUREMENT_CONTRACT_EPOCH_MISMATCH", detail=str(identifier)
                )
            body = record.get("body", {})
            freshness = int(
                body.get("freshness_bound_ms", body.get("freshnessBoundMs", 0))
            )
            clock = body.get("clock_requirement", body.get("clockRequirement"))
            if freshness <= 0 or not clock:
                raise KernelRefusal(
                    "KERNEL_WATCHDOG_STALENESS_UNBOUNDED", detail=str(identifier)
                )
        transaction_id = trial["transactionId"]
        leases = [
            envelope.payload.get("leaseExpiry")
            for envelope in self._event_store.iterate()
            if envelope.event_kind == "TokenIssued"
            and envelope.payload.get("transactionId") == transaction_id
            and envelope.payload.get("tokenKind") == TokenKind.READY.value
        ]
        if not any(
            isinstance(lease, str) and parse_utc(lease) > parse_utc(now)
            for lease in leases
        ):
            raise KernelRefusal("KERNEL_WATCHDOG_NO_LIVE_LEASE")
        return KERNEL_HOSTED_WATCHDOG_GUARDS

    def decide_trial(
        self,
        trial_id: str,
        *,
        stop_reasons: Sequence[StopReason],
        now: str,
    ) -> TrialState:
        """Apply safety precedence to the durable Kernel evaluation."""
        state = self._state()
        try:
            trial = state["trials"][trial_id]
        except KeyError as exc:
            raise KernelRefusal("UNKNOWN_TRIAL", detail=trial_id) from exc
        if trial["state"] != TrialState.DECISION_HOLD.value:
            raise KernelRefusal("DECISION_OUTSIDE_HOLD")
        if any(not isinstance(reason, StopReason) for reason in stop_reasons):
            raise TypeError("stop_reasons values must be StopReason")
        evaluation = trial["evaluation"]
        execution_validity = ExecutionValidity(evaluation["executionValidity"])
        measurement_sufficiency = MeasurementSufficiency(
            evaluation["measurementSufficiency"]
        )
        predicate_verdicts = {
            key: PredicateVerdict(value)
            for key, value in evaluation["predicateVerdicts"].items()
        }
        mandatory_ids = tuple(evaluation.get("mandatoryPredicateIds", []))
        derived_reasons = list(stop_reasons)
        if trial.get("stopReason") is not None:
            derived_reasons.append(StopReason(trial["stopReason"]))
        if execution_validity is ExecutionValidity.EXEC_ERROR:
            derived_reasons.append(StopReason.EXECUTION_ERROR)
        elif execution_validity is ExecutionValidity.INVALID:
            derived_reasons.append(StopReason.VALIDITY_EXIT)
        if measurement_sufficiency is MeasurementSufficiency.STALE:
            derived_reasons.append(StopReason.TELEMETRY_STALE)
        stop_reason = strongest_reason(derived_reasons) if derived_reasons else None
        if stop_reason is not None and stop_reason is not StopReason.BASELINE_RESET:
            return self.advance_trial(
                trial_id, TrialState.STOPPING, reason=stop_reason, now=now
            )
        success = (
            execution_validity is ExecutionValidity.VALID
            and measurement_sufficiency is MeasurementSufficiency.SUFFICIENT
            and bool(predicate_verdicts)
            and all(
                predicate_verdicts.get(predicate_id) is PredicateVerdict.PASS
                for predicate_id in mandatory_ids
            )
            and evaluation.get("holdComplete")
            and evaluation.get("sameCandidate")
            and evaluation.get("validityRegionStable")
        )
        if success and stop_reason is StopReason.BASELINE_RESET:
            # Case policy (2026-09-20): a passing trial is still returned to the
            # frozen baseline through the ordinary rollback path; its verdict is
            # kept (TrialOutcome.PASS_RESET), nothing is finalized live.
            return self.advance_trial(
                trial_id, TrialState.STOPPING, reason=StopReason.BASELINE_RESET, now=now
            )
        if success:
            return self.advance_trial(trial_id, TrialState.FINALIZING_LIVE, now=now)
        return self.advance_trial(
            trial_id,
            TrialState.STOPPING,
            reason=StopReason.SEMANTIC_NON_SUCCESS,
            now=now,
        )

    def _lease_ms(
        self, state: Mapping[str, Any], trial: Mapping[str, Any], token_kind: Any
    ) -> int:
        """How long this permit must live; see :func:`contracted_lease_ms`.

        Inside :meth:`recover` every permit is stamped with the pass's one
        ``now`` (the Kernel reads no clock), so a permit issued after earlier
        gateway calls of the same pass must also cover the time those calls
        were allowed to take -- the sum of the leases issued before it, plus one
        default per transaction query.  2026-09-23 audit: with several uncertain
        transactions the later ones' rollback permits arrived already spent.
        Outside a recovery pass nothing is added.  Not reduced state: only the
        recorded ``leaseExpiry`` value changes, never a key.
        """
        lease = contracted_lease_ms(state, trial, token_kind)
        spent = getattr(self, "_recovery_spent_ms", None)
        if spent is None:
            return lease
        self._recovery_spent_ms = spent + lease
        return lease + spent

    def issue_token(self, trial_id: str, *, token_kind: Any, now: str) -> Any:
        """Issue the Write Gateway token for the trial's next action.

        Signature frozen; body owned by lane **KERN**.

        Returns a :class:`~assurance.gateway.token.KernelToken` bound to the
        transaction, trial, fencing token, command sequence, lease, expected
        configuration hash and idempotency key.

        The fencing token must be strictly increasing per resource, so a
        delayed command from a previous attempt is refused by the gateway
        rather than applied late (task section 6.12's old-fence rule).  A
        token is a deterministic safety permit; it is not an approval and
        nobody signs it (design section 4.4).
        """
        self._ensure_initialized()
        if not isinstance(token_kind, TokenKind):
            raise KernelRefusal("UNKNOWN_TOKEN_KIND")
        state = self._state()
        try:
            trial = state["trials"][trial_id]
        except KeyError as exc:
            raise KernelRefusal("UNKNOWN_TRIAL", detail=trial_id) from exc
        trial_state = TrialState(trial["state"])
        allowed_states = {
            TokenKind.PREPARE: {TrialState.PREPARING},
            TokenKind.READY: {TrialState.PREPARING, TrialState.READY},
            TokenKind.COMMIT: {TrialState.COMMIT_DECIDED},
            TokenKind.STOP: {
                TrialState.COMMIT_DECIDED,
                TrialState.APPLYING,
                TrialState.APPLIED_PENDING_RESULT,
                TrialState.SETTLING,
                TrialState.OBSERVING,
                TrialState.DECISION_HOLD,
                TrialState.FINALIZING_LIVE,
                TrialState.STOPPING,
                TrialState.RECOVERY_VERIFYING,
            },
            TokenKind.REVERSE_ROLLBACK: {
                TrialState.STOPPING,
                TrialState.REVERSE_ROLLBACK,
                TrialState.RECOVERY_VERIFYING,
            },
            TokenKind.CONFIGURATION_REREAD: {
                TrialState.FINALIZING_LIVE,
                # A reread writes nothing, and these are exactly the states in
                # which the Kernel must learn the live configuration before it
                # may name one on a reverse-rollback permit.  Without it the
                # gateway answers "the permit names a configuration that is
                # not live; reread first" and a recoverable transaction is
                # locked down instead.
                TrialState.STOPPING,
                TrialState.REVERSE_ROLLBACK,
                TrialState.RECOVERY_VERIFYING,
            },
            TokenKind.RECOVERY_CONFIRM: {TrialState.RECOVERY_VERIFYING},
            TokenKind.EMERGENCY_SAFE_STATE: set(TrialState),
            TokenKind.FINALIZE_LIVE: {TrialState.FINALIZING_LIVE},
        }
        if trial_state not in allowed_states[token_kind]:
            raise KernelRefusal(
                "TOKEN_KIND_NOT_ALLOWED_IN_STATE",
                detail=f"{token_kind.value}@{trial_state.value}",
            )
        resource_id = trial["resourceId"]
        fence = int(state["fences"].get(resource_id, -1)) + 1
        transaction_id = trial["transactionId"]
        command_sequence = sum(
            1
            for envelope in self._event_store.iterate()
            if envelope.event_kind == "TokenIssued"
            and envelope.payload.get("transactionId") == transaction_id
        )
        # ``expected_config_hash`` is "what the gateway must observe *before*
        # acting" (assurance/gateway/token.py).  That is a *configuration*
        # digest, so it is the last configuration this trial's gateway read
        # back, and before any readback the baseline of the plan the Kernel
        # staged.  It is deliberately not the candidate's semantic hash: the
        # meaning of a candidate and the digest of a configuration are
        # different values, and naming the first would make every gateway
        # operation a REJECTED_CONFIG_MISMATCH.
        expected_config_hash = trial.get("observedConfigHash") or trial.get(
            "planBaselineHash"
        )
        if expected_config_hash is None:
            if token_kind is not TokenKind.EMERGENCY_SAFE_STATE:
                raise KernelRefusal("ACTUATION_PLAN_NOT_STAGED", detail=trial_id)
            # An emergency safe state must work from any state, including one
            # where nothing was ever staged, and drives to the deployment's
            # contracted safe configuration without comparing against a
            # permit.  The digest is carried for correlation only.
            expected_config_hash = trial["candidateSemanticHash"]
        token = KernelToken(
            token_kind=token_kind,
            transaction_id=transaction_id,
            trial_id=trial_id,
            fencing_token=fence,
            command_sequence=command_sequence,
            lease_expiry=format_utc(
                parse_utc(now)
                + timedelta(milliseconds=self._lease_ms(state, trial, token_kind))
            ),
            expected_config_hash=expected_config_hash,
            idempotency_key=f"gateway:{transaction_id}:{fence}:{token_kind.value}",
            issued_at=now,
        )
        self._append(
            "TokenIssued",
            object_id=trial_id,
            now=now,
            payload={
                **token.to_canonical_dict(),
                "resourceId": resource_id,
            },
            idempotency_key=f"token:{token.content_hash()}",
        )
        return token

    def record_gateway_result(
        self,
        token: KernelToken,
        result: GatewayResult,
        *,
        resource_id: str,
        now: str,
    ) -> EventEnvelope:
        """Record a gateway observation after lease and fence validation."""
        if not isinstance(token, KernelToken) or not isinstance(result, GatewayResult):
            raise TypeError("token and result must be gateway dataclasses")
        state = self._state()
        trial = state["trials"].get(token.trial_id)
        canonical_token = token.to_canonical_dict()
        issued = any(
            envelope.event_kind == "TokenIssued"
            and envelope.payload.get("resourceId") == resource_id
            and all(envelope.payload.get(key) == value for key, value in canonical_token.items())
            for envelope in self._event_store.iterate()
        )
        if (
            trial is None
            or trial.get("transactionId") != token.transaction_id
            or trial.get("resourceId") != resource_id
            or not issued
        ):
            self._append(
                "GatewayResultRejected",
                object_id=token.trial_id,
                now=now,
                payload={
                    "fencingToken": token.fencing_token,
                    "reason": "TOKEN_NOT_ISSUED",
                    "resourceId": resource_id,
                    "transactionId": token.transaction_id,
                    "trialId": token.trial_id,
                },
            )
            raise KernelRefusal("TOKEN_NOT_ISSUED")
        current_fence = int(state["fences"].get(resource_id, -1))
        if token.fencing_token < current_fence:
            self._append(
                "GatewayResultRejected",
                object_id=token.trial_id,
                now=now,
                payload={
                    "fencingToken": token.fencing_token,
                    "reason": "OLD_FENCE",
                    "resourceId": resource_id,
                    "transactionId": token.transaction_id,
                    "trialId": token.trial_id,
                },
            )
            raise KernelRefusal("OLD_FENCE")
        if token.is_expired(now):
            self._append(
                "GatewayResultRejected",
                object_id=token.trial_id,
                now=now,
                payload={
                    "fencingToken": token.fencing_token,
                    "reason": "LEASE_EXPIRED",
                    "resourceId": resource_id,
                    "transactionId": token.transaction_id,
                    "trialId": token.trial_id,
                },
            )
            current = TrialState(self._state()["trials"][token.trial_id]["state"])
            if TrialState.STOPPING in self._successors(current):
                self.advance_trial(
                    token.trial_id,
                    TrialState.STOPPING,
                    reason=StopReason.LEASE_EXPIRY,
                    now=now,
                )
            raise KernelRefusal("LEASE_EXPIRED")
        envelope = self._append(
            "GatewayResultRecorded",
            object_id=token.trial_id,
            now=now,
            payload={
                "evidenceRefs": list(result.evidence_refs),
                "expectedConfigHash": token.expected_config_hash,
                "fencingToken": token.fencing_token,
                "observedConfigHash": result.observed_config_hash,
                "outcome": result.outcome.value,
                "resourceId": resource_id,
                "tokenKind": token.token_kind.value,
                "transactionId": token.transaction_id,
                "trialId": token.trial_id,
            },
            idempotency_key=f"gateway-result:{token.content_hash()}:{result.outcome.value}",
        )
        safety_result_reasons = {
            GatewayOutcome.PARTIAL_APPLY: StopReason.PARTIAL_APPLY,
            GatewayOutcome.UNKNOWN: StopReason.PARTIAL_APPLY,
            GatewayOutcome.ERROR: StopReason.EXECUTION_ERROR,
            GatewayOutcome.REJECTED_LEASE_EXPIRED: StopReason.LEASE_EXPIRY,
            GatewayOutcome.REJECTED_CONFIG_MISMATCH: StopReason.PARTIAL_APPLY,
            GatewayOutcome.REJECTED_IDEMPOTENCY_COLLISION: StopReason.EXECUTION_ERROR,
        }
        if result.outcome in safety_result_reasons:
            current = TrialState(self._state()["trials"][token.trial_id]["state"])
            if TrialState.STOPPING in self._successors(current):
                self.advance_trial(
                    token.trial_id,
                    TrialState.STOPPING,
                    reason=safety_result_reasons[result.outcome],
                    now=now,
                )
        return envelope

    @staticmethod
    def _successors(state: TrialState) -> Tuple[TrialState, ...]:
        from assurance.core.states import successors

        return tuple(successors(state))

    # ------------------------------------------------------------------ #
    # Reserve, locks, harm accounting (design section 7 steps 5 and 10)
    # ------------------------------------------------------------------ #

    def reserve(self, trial_id: str, *, now: str) -> EventEnvelope:
        """Reserve harm budget and acquire resource locks for a trial.

        Signature frozen; body owned by lane **KERN**.

        Reserve is taken from the epoch-frozen harm contract, against the
        append-only Harm Ledger keyed by contract, case and epoch.  A new
        release or epoch must not reset it -- that is the cutover for GAP-05
        in ``docs/architecture/GATE1-MAP.md``, where a fresh reserve ledger was
        constructed per episode.

        Locks acquired here are held through stop, reverse rollback and
        recovery reread, and released only by :meth:`settle_trial` (GAP-04).
        """
        state = self._state()
        try:
            trial = state["trials"][trial_id]
            case = state["cases"][trial["caseId"]]
        except KeyError as exc:
            raise KernelRefusal("UNKNOWN_TRIAL", detail=trial_id) from exc
        if trial["state"] != TrialState.VALIDATING.value:
            raise KernelRefusal("RESERVE_OUTSIDE_VALIDATION")
        lock_owner = state["resourceLocks"].get(trial["resourceId"])
        if lock_owner is not None and lock_owner != trial_id:
            raise KernelRefusal("RESOURCE_LOCKED", detail=str(lock_owner))
        refs = list(case.get("harmContractRefs", []))
        if not refs:
            raise KernelRefusal("NO_HARM_CONTRACT")
        reservations = []
        for harm_contract_ref in refs:
            amount = case.get("reservePerTrial", {}).get(harm_contract_ref)
            usable = case.get("usableReserve", {}).get(harm_contract_ref)
            if not isinstance(amount, Mapping) or not isinstance(usable, Mapping):
                raise KernelRefusal("NO_TRIAL_RESERVE", detail=harm_contract_ref)
            if amount.get("unit") != usable.get("unit"):
                raise KernelRefusal("HARM_UNIT_MISMATCH", detail=harm_contract_ref)
            requested = float(amount.get("value", 0))
            total = float(usable.get("value", 0))
            reserved = sum(
                float(entry["amount"]["value"])
                for entry in state["harmLedger"]
                if entry.get("caseId") == trial["caseId"]
                and entry.get("harmContractRef") == harm_contract_ref
                and entry.get("harmKind")
                == case.get("harmKinds", {}).get(
                    harm_contract_ref, HarmKind.TRIAL_INDUCED.value
                )
                and entry.get("movementKind") == "RESERVE"
                and entry.get("amount", {}).get("unit") == amount.get("unit")
            )
            returned = sum(
                float(entry["amount"]["value"])
                for entry in state["harmLedger"]
                if entry.get("caseId") == trial["caseId"]
                and entry.get("harmContractRef") == harm_contract_ref
                and entry.get("harmKind")
                == case.get("harmKinds", {}).get(
                    harm_contract_ref, HarmKind.TRIAL_INDUCED.value
                )
                and entry.get("movementKind") == "RETURN"
                and entry.get("amount", {}).get("unit") == amount.get("unit")
            )
            if requested <= 0 or requested > total - reserved + returned:
                raise KernelRefusal(
                    "USABLE_RESERVE_EXHAUSTED", detail=harm_contract_ref
                )
            reservations.append(
                {
                    "amount": dict(amount),
                    "caseId": trial["caseId"],
                    "chargedForMissingInterval": False,
                    "epochId": trial["epochId"],
                    "harmContractRef": harm_contract_ref,
                    "harmKind": case.get("harmKinds", {}).get(
                        harm_contract_ref, HarmKind.TRIAL_INDUCED.value
                    ),
                    "movementKind": "RESERVE",
                    "reason": "trial_reserve",
                }
            )
        return self._append(
            "HarmReserved",
            object_id=f"resource-lock:{trial['resourceId']}",
            now=now,
            payload={
                "caseId": trial["caseId"],
                "epochId": trial["epochId"],
                "reservations": reservations,
                "resourceIds": [trial["resourceId"]],
                "trialId": trial_id,
            },
            idempotency_key=f"reserve:{trial_id}",
        )

    def charge_harm(
        self,
        trial_id: str,
        *,
        amount: Any,
        harm_kind: Any,
        for_missing_interval: bool,
        now: str,
    ) -> EventEnvelope:
        """Charge harm against the reserve.

        Signature frozen; body owned by lane **KERN**.

        *amount* is a :class:`~assurance.core.provenance.TypedQuantity`, so
        the ledger records the unit and provenance of every charge.
        *for_missing_interval* marks a conservative contract-defined
        substitute rather than an observation -- design section 8 forbids zero
        or last-value substitution for a gap, and the paper reports how much
        of the total charge was assumed rather than measured.

        Target debt is charged under its own
        :class:`~assurance.contracts.harm.HarmKind` and never nets against
        trial-induced or contract harm (task section 5.7).
        """
        self._ensure_initialized()
        from assurance.core.provenance import TypedQuantity

        if not isinstance(amount, TypedQuantity):
            raise TypeError("amount must be TypedQuantity")
        if not isinstance(harm_kind, HarmKind):
            raise TypeError("harm_kind must be HarmKind")
        try:
            amount.require_admissible("harm charge")
        except ValueError as exc:
            raise KernelRefusal("INADMISSIBLE_HARM_CHARGE", detail=str(exc)) from exc
        if amount.value < 0:
            raise KernelRefusal("NEGATIVE_HARM_CHARGE")
        if for_missing_interval and amount.value <= 0:
            raise KernelRefusal("NON_CONSERVATIVE_MISSING_CHARGE")
        state = self._state()
        try:
            trial = state["trials"][trial_id]
            case = state["cases"][trial["caseId"]]
        except KeyError as exc:
            raise KernelRefusal("UNKNOWN_TRIAL", detail=trial_id) from exc
        if not trial.get("applyCounted"):
            raise KernelRefusal("HARM_BEFORE_APPLY")
        refs = list(case.get("harmContractRefs", []))
        if not refs:
            raise KernelRefusal("NO_HARM_CONTRACT")
        matching_reservations = [
            entry
            for entry in state["harmLedger"]
            if entry.get("trialId") == trial_id
            and entry.get("movementKind") == "RESERVE"
            and entry.get("harmKind") == harm_kind.value
            and entry.get("amount", {}).get("unit") == amount.unit
        ]
        if not matching_reservations:
            raise KernelRefusal("HARM_BUCKET_NOT_RESERVED")
        matching_refs = {
            str(entry["harmContractRef"]) for entry in matching_reservations
        }
        if len(matching_refs) != 1:
            raise KernelRefusal("HARM_CONTRACT_AMBIGUOUS")
        harm_contract_ref = next(iter(matching_refs))
        reserved = sum(
            float(entry["amount"]["value"])
            for entry in state["harmLedger"]
            if entry.get("trialId") == trial_id
            and entry.get("movementKind") == "RESERVE"
            and entry.get("harmContractRef") == harm_contract_ref
            and entry.get("harmKind") == harm_kind.value
            and entry.get("amount", {}).get("unit") == amount.unit
        )
        charged = sum(
            float(entry["amount"]["value"])
            for entry in state["pendingHarmCharges"].get(trial_id, [])
            if entry.get("harmContractRef") == harm_contract_ref
            and entry.get("harmKind") == harm_kind.value
            and entry.get("amount", {}).get("unit") == amount.unit
        )
        if float(amount.value) > reserved - charged:
            current = TrialState(trial["state"])
            if TrialState.STOPPING in self._successors(current):
                self.advance_trial(
                    trial_id,
                    TrialState.STOPPING,
                    reason=StopReason.HARM_LIMIT_BREACH,
                    now=now,
                )
            raise KernelRefusal("TRIAL_RESERVE_EXCEEDED")
        return self._append(
            "HarmCharged",
            object_id=trial_id,
            now=now,
            payload={
                "amount": amount.to_canonical_dict(),
                "caseId": trial["caseId"],
                "chargedForMissingInterval": bool(for_missing_interval),
                "epochId": trial["epochId"],
                "harmContractRef": harm_contract_ref,
                "harmKind": harm_kind.value,
                "movementKind": "CHARGE",
                "reason": (
                    "conservative_missing_interval"
                    if for_missing_interval
                    else "observed_harm"
                ),
                "trialId": trial_id,
            },
        )

    def _remaining_reserve(
        self, case_id: str, *, state: Optional[Mapping[str, Any]] = None
    ) -> Optional[float]:
        reduced = state if state is not None else self._state()
        case = reduced["cases"].get(case_id)
        if case is None:
            return None
        balances = []
        for harm_contract_ref, quantity in case.get("usableReserve", {}).items():
            unit = quantity.get("unit")
            charged = sum(
                float((entry.get("amount") or {}).get("value", 0))
                for entry in reduced["harmLedger"]
                if entry.get("caseId") == case_id
                and entry.get("harmContractRef") == harm_contract_ref
                and entry.get("harmKind")
                == case.get("harmKinds", {}).get(
                    harm_contract_ref, HarmKind.TRIAL_INDUCED.value
                )
                and entry.get("movementKind") == "CHARGE"
                and entry.get("amount", {}).get("unit") == unit
            ) + sum(
                float((entry.get("amount") or {}).get("value", 0))
                for entries in reduced["pendingHarmCharges"].values()
                for entry in entries
                if entry.get("caseId") == case_id
                and entry.get("harmContractRef") == harm_contract_ref
                and entry.get("harmKind")
                == case.get("harmKinds", {}).get(
                    harm_contract_ref, HarmKind.TRIAL_INDUCED.value
                )
                and entry.get("movementKind") == "CHARGE"
                and entry.get("amount", {}).get("unit") == unit
            )
            balances.append(float(quantity.get("value", 0)) - charged)
        return min(balances) if balances else None

    # ------------------------------------------------------------------ #
    # Measurement and evaluation (design sections 4.5, 8)
    # ------------------------------------------------------------------ #

    def ingest_raw_sample(self, sample: Any, *, now: str) -> EventEnvelope:
        """Accept one :class:`~assurance.collector.samples.RawSample`.

        Signature frozen; body owned by lane **KERN**.

        Called by the Measurement Collector directly.  Design section 4.5:
        "Agent summaries cannot substitute for raw measurements" -- so this
        method must refuse a sample whose source component is not
        :attr:`~assurance.core.components.ComponentId.MEASUREMENT_COLLECTOR`,
        and must refuse a value whose provenance is not ``MEASURED``.
        """
        self._ensure_initialized()
        from assurance.collector.samples import RawSample
        from assurance.core.provenance import Provenance

        if not isinstance(sample, RawSample):
            raise KernelRefusal("RAW_SAMPLE_TYPE_REQUIRED")
        if sample.source_component is not ComponentId.MEASUREMENT_COLLECTOR:
            raise KernelRefusal("RAW_SAMPLE_SOURCE_REQUIRED")
        if sample.value.provenance is not Provenance.MEASURED:
            raise KernelRefusal("RAW_SAMPLE_PROVENANCE_REQUIRED")
        # Read-only admission facts: borrow the accumulator under its lock.
        # A deepcopy here cost O(events) per sample -- 37 ms each by the end of
        # a live trial (2026-09-15 attempt 40), so 12 counters at 1 Hz fell
        # 20-33 s behind and the trial outlived its A1 policy window.
        with self._replay_lock:
            state = self._current_state()
            if sample.sample_id in state["samples"]:
                raise KernelRefusal("DUPLICATE_RAW_SAMPLE")
            # 2026-09-22: 이 max() 는 샘플 하나를 받을 때마다 **전체 샘플**을 훑었다.
            # counter 가 달라도 검사하므로 총 비용이 O(S^2) 다 -- 샘플 1만 개면 비교가
            # 약 5천만 회이고, 시행이 길어질수록 자라는 종류의 비용이다.  바로 위 주석의
            # deepcopy 전례와 같은 자리, 같은 실수다.
            # counter 별 마지막 sequence 를 증분으로 들고 간다.  샘플은 오직 이 경로로만
            # 들어오므로 증분 갱신이 정확하고, 샘플 수가 기대와 어긋나면(재생·되감기)
            # 한 번 다시 훑어 스스로 복구한다.
            samples = state["samples"]
            index = getattr(self, "_last_sequence_by_counter", None)
            if index is None or getattr(self, "_last_sequence_count", -1) != len(samples):
                index = {}
                for item in samples.values():
                    counter = item.get("counterId")
                    sequence = int(item["sequence"])
                    if sequence > index.get(counter, -1):
                        index[counter] = sequence
                self._last_sequence_by_counter = index
                self._last_sequence_count = len(samples)
            last_sequence = index.get(sample.counter_id, -1)
        if sample.sequence <= last_sequence:
            raise KernelRefusal("STALE_RAW_SAMPLE")
        if sample.sequence > last_sequence + 1:
            raise KernelRefusal("REORDERED_RAW_SAMPLE")
        appended = self._append(
            "RawSampleIngested",
            object_id=f"sample:{sample.counter_id}",
            now=now,
            payload={**sample.to_canonical_dict(), "ingestedAt": now},
            idempotency_key=f"raw-sample:{sample.sample_id}",
        )
        # 받아들여진 샘플만 지표를 옮긴다.  거절된 샘플은 상태에 없으므로 세지 않는다.
        index = getattr(self, "_last_sequence_by_counter", None)
        if index is not None:
            index[sample.counter_id] = sample.sequence
            self._last_sequence_count = getattr(self, "_last_sequence_count", 0) + 1
        return appended

    def evaluate_trial(
        self, trial_id: str, *, now: str
    ) -> Tuple[ExecutionValidity, MeasurementSufficiency, Mapping[str, PredicateVerdict]]:
        """Evaluate a trial's raw traces against its contracts.

        Signature frozen; body owned by lane **KERN**.

        Returns the three axes separately, and the predicate verdicts per
        predicate id.  They are returned as a tuple rather than a single
        status precisely because design section 8 forbids collapsing them:
        a caller that wants "did it pass" has to look at all three, which is
        the intended friction.

        Must evaluate from raw traces only.  An agent's summary, an
        explanation, a confidence value and a free-text rationale are not
        inputs -- that is the cutover for GAP-02 in
        ``docs/architecture/GATE1-MAP.md``.
        """
        from assurance.collector.samples import ClockHealth
        from assurance.contracts.measurement import (
            Aggregation,
            ClockRequirement,
            Estimator,
            OverlapPolicy,
        )
        from assurance.contracts.target import ComparisonOperator
        from assurance.core.provenance import (
            DocumentStatus,
            Provenance,
            TypedQuantity,
        )

        state = self._state()
        try:
            trial = state["trials"][trial_id]
        except KeyError as exc:
            raise KernelRefusal("UNKNOWN_TRIAL", detail=trial_id) from exc
        if not trial.get("applyCounted"):
            raise KernelRefusal("EVALUATION_BEFORE_APPLY")

        trial_epoch = state["epochs"].get(trial["epochId"])
        if trial_epoch is None:
            raise KernelRefusal("TRIAL_EPOCH_NOT_RESOLVED")
        target_hash = trial_epoch.get("targetContractHashes", {}).get(
            trial["targetRef"]
        )
        target_record = state["contracts"].get(target_hash)
        if target_record is None:
            raise KernelRefusal("TARGET_CONTRACT_NOT_RESOLVED")
        if target_record.get("family") != "TargetContract":
            raise KernelRefusal("TARGET_CONTRACT_EPOCH_MISMATCH")
        target = target_record["body"]
        measurements: Dict[str, Mapping[str, Any]] = {}
        for identifier, contract_hash in trial_epoch.get(
            "measurementContractHashes", {}
        ).items():
            record = state["contracts"].get(contract_hash)
            if (
                record is None
                or record.get("family") != "MeasurementContract"
            ):
                raise KernelRefusal(
                    "MEASUREMENT_CONTRACT_EPOCH_MISMATCH", detail=str(identifier)
                )
            measurements[str(identifier)] = record["body"]

        stop_reason = trial.get("stopReason")
        if stop_reason == StopReason.EXECUTION_ERROR.value:
            validity = ExecutionValidity.EXEC_ERROR
        elif stop_reason in {
            StopReason.PARTIAL_APPLY.value,
            StopReason.VALIDITY_EXIT.value,
            StopReason.HARD_SAFETY_GUARD.value,
            StopReason.LEASE_EXPIRY.value,
        }:
            validity = ExecutionValidity.INVALID
        else:
            validity = ExecutionValidity.VALID

        started_at = trial.get("observationStartedAt")
        if started_at is None:
            raise KernelRefusal("OBSERVATION_WINDOW_NOT_STARTED")
        samples = [
            sample
            for sample in state["samples"].values()
            if started_at is not None
            and parse_utc(started_at) <= parse_utc(sample["observedAt"]) <= parse_utc(now)
        ]
        verdicts: Dict[str, PredicateVerdict] = {}
        mandatory_predicate_ids: list[str] = []
        insufficiencies: list[MeasurementSufficiency] = []
        hold_requirements = [int(target.get("hold_ms", target.get("holdMs", 0)))]
        used_trace_refs: set[str] = set()
        elapsed_observation_ms = int(
            (parse_utc(now) - parse_utc(started_at)).total_seconds() * 1000
        )

        def completed_window_indices(
            measurement: Mapping[str, Any], hold_ms: int
        ) -> Tuple[int, ...]:
            width = int(
                measurement.get(
                    "window_width_ms", measurement.get("windowWidthMs", 0)
                )
            )
            stride = int(
                measurement.get(
                    "window_stride_ms", measurement.get("windowStrideMs", 0)
                )
            )
            overlap = measurement.get("overlap")
            if width <= 0 or stride <= 0 or (
                overlap == OverlapPolicy.DISJOINT.value and stride < width
            ):
                raise KernelRefusal("INVALID_MEASUREMENT_WINDOW_GEOMETRY")
            if elapsed_observation_ms < width:
                return ()
            latest_index = (elapsed_observation_ms - width) // stride
            if hold_ms <= 0:
                return (latest_index,)
            hold_start = max(0, elapsed_observation_ms - hold_ms)
            return tuple(
                index
                for index in range(latest_index + 1)
                if index * stride + width > hold_start
            )

        def measured_value(
            measurement: Mapping[str, Any], window_index: int
        ) -> Tuple[MeasurementSufficiency, Optional[float]]:
            counter_id = measurement.get("counter_id", measurement.get("counterId"))
            cadence = int(measurement.get("cadence_ms", measurement.get("cadenceMs", 0)))
            width = int(
                measurement.get("window_width_ms", measurement.get("windowWidthMs", 0))
            )
            stride = int(
                measurement.get("window_stride_ms", measurement.get("windowStrideMs", 0))
            )
            overlap = measurement.get("overlap")
            if width <= 0 or stride <= 0 or (
                overlap == OverlapPolicy.DISJOINT.value and stride < width
            ):
                raise KernelRefusal("INVALID_MEASUREMENT_WINDOW_GEOMETRY")
            window_start = parse_utc(started_at) + timedelta(
                milliseconds=window_index * stride
            )
            window_end = window_start + timedelta(milliseconds=width)
            relevant = [
                sample
                for sample in samples
                if sample["counterId"] == counter_id
                and window_start <= parse_utc(sample["observedAt"]) <= window_end
            ]
            if not relevant:
                return MeasurementSufficiency.INSUFFICIENT_COVERAGE, None
            used_trace_refs.update(
                str(sample["traceHash"]) for sample in relevant
            )
            if cadence <= 0 or any(
                int(sample.get("cadenceMs", -1)) != cadence for sample in relevant
            ):
                return MeasurementSufficiency.INSUFFICIENT_COVERAGE, None
            selector = measurement.get("scope_selector", measurement.get("scopeSelector", {}))
            if any(
                any(sample["scopeSnapshot"].get(key) != value for key, value in selector.items())
                for sample in relevant
            ):
                return MeasurementSufficiency.SCOPE_MISMATCH, None
            membership = {
                str(item)
                for item in measurement.get(
                    "membership_snapshot", measurement.get("membershipSnapshot", [])
                )
            }
            if membership:
                observed_members = {
                    str(sample["scopeSnapshot"].get("ueId"))
                    for sample in relevant
                    if sample["scopeSnapshot"].get("ueId") is not None
                }
                if observed_members != membership:
                    return MeasurementSufficiency.SCOPE_MISMATCH, None
            if any(sample.get("missingIntervals") for sample in relevant):
                return MeasurementSufficiency.MISSING_INTERVAL, None
            clock_requirement = measurement.get(
                "clock_requirement", measurement.get("clockRequirement")
            )
            allowed_clocks = {ClockHealth.SYNCHRONISED.value}
            if clock_requirement == ClockRequirement.DRIFT_BOUNDED.value:
                allowed_clocks.add(ClockHealth.DRIFTING_WITHIN_BOUND.value)
            if any(sample["clockHealth"] not in allowed_clocks for sample in relevant):
                return MeasurementSufficiency.CLOCK_UNHEALTHY, None
            freshness = int(
                measurement.get(
                    "freshness_bound_ms", measurement.get("freshnessBoundMs", 0)
                )
            )
            latest = max(parse_utc(sample["observedAt"]) for sample in relevant)
            # Freshness asks whether the telemetry behind *this window* is
            # current, so the instant it is measured against is the window's
            # own end once the window has closed, and ``now`` while the window
            # is still open.  Measuring a closed window against the evaluation
            # clock made every earlier window of a hold stale by construction
            # as soon as ``hold_ms`` outran the bound: the live 2026-09-12
            # episode held for 6000 ms against a 2000 ms bound and was locked
            # down on a 1 Hz stream with no gaps, where a genuinely dead
            # collector would have looked exactly the same.  The guard is not
            # weakened -- a closed window whose last sample predates its end by
            # more than the bound is still STALE.
            #
            # Two questions, not one.  This window's own content is judged
            # against the window; whether the stream is still alive is judged
            # against the evaluation clock, over the newest sample of the
            # counter anywhere in the trial.  Collapsing them into a single
            # ``now``-relative test is what produced the 2026-09-12 lockdown;
            # dropping the second one is what let a collector that had stopped
            # delivering read as INSUFFICIENT_COVERAGE instead of STALE.
            reference = min(parse_utc(now), window_end)
            age_ms = (reference - latest).total_seconds() * 1000
            if age_ms < 0 or age_ms > freshness:
                return MeasurementSufficiency.STALE, None
            newest = max(
                parse_utc(sample["observedAt"])
                for sample in samples
                if sample["counterId"] == counter_id
            )
            stream_age_ms = (parse_utc(now) - newest).total_seconds() * 1000
            if stream_age_ms < 0 or stream_age_ms > freshness:
                return MeasurementSufficiency.STALE, None
            entity_count = len(
                {
                    tuple(sorted(sample["scopeSnapshot"].items())) for sample in relevant
                }
            )
            minimum = int(
                measurement.get(
                    "minimum_entity_count", measurement.get("minimumEntityCount", 1)
                )
            )
            if entity_count < minimum:
                return MeasurementSufficiency.INSUFFICIENT_COVERAGE, None
            first = min(parse_utc(sample["observedAt"]) for sample in relevant)
            if (latest - first).total_seconds() * 1000 < width:
                return MeasurementSufficiency.INSUFFICIENT_COVERAGE, None
            ordered_times = sorted(
                {parse_utc(sample["observedAt"]) for sample in relevant}
            )
            if any(
                (right - left).total_seconds() * 1000 > cadence
                for left, right in zip(ordered_times, ordered_times[1:])
            ):
                return MeasurementSufficiency.INSUFFICIENT_COVERAGE, None
            values = [float(sample["value"]["value"]) for sample in relevant]
            aggregation = measurement.get("aggregation")
            estimator = measurement.get("estimator")
            compatible_estimators = {
                Aggregation.MEAN.value: {
                    Estimator.SAMPLE_MEAN.value,
                    Estimator.TRIMMED_MEAN.value,
                },
                Aggregation.MEDIAN.value: {
                    Estimator.EMPIRICAL_QUANTILE.value,
                    Estimator.INTERPOLATED_QUANTILE.value,
                },
                Aggregation.P95.value: {
                    Estimator.EMPIRICAL_QUANTILE.value,
                    Estimator.INTERPOLATED_QUANTILE.value,
                },
                Aggregation.P99.value: {
                    Estimator.EMPIRICAL_QUANTILE.value,
                    Estimator.INTERPOLATED_QUANTILE.value,
                },
                Aggregation.RATIO.value: {Estimator.RATIO_OF_SUMS.value},
            }
            allowed = compatible_estimators.get(aggregation)
            if allowed is not None and estimator not in allowed:
                raise KernelRefusal("INCOMPATIBLE_MEASUREMENT_ESTIMATOR")
            if aggregation == Aggregation.MIN.value:
                aggregate = min(values)
            elif aggregation == Aggregation.MAX.value:
                aggregate = max(values)
            elif aggregation == Aggregation.SUM.value:
                aggregate = sum(values)
            elif aggregation == Aggregation.COUNT.value:
                aggregate = float(len(values))
            elif aggregation == Aggregation.RATIO.value:
                raise KernelRefusal("RATIO_MEASUREMENT_REQUIRES_TYPED_NUMERATOR")
            elif aggregation in {Aggregation.P95.value, Aggregation.P99.value}:
                ordered = sorted(values)
                percentile = 0.95 if aggregation == Aggregation.P95.value else 0.99
                position = percentile * (len(ordered) - 1)
                lower = int(position)
                upper = min(len(ordered) - 1, lower + 1)
                if estimator == Estimator.INTERPOLATED_QUANTILE.value:
                    fraction = position - lower
                    aggregate = ordered[lower] + fraction * (
                        ordered[upper] - ordered[lower]
                    )
                else:
                    index = min(len(ordered) - 1, int(percentile * len(ordered)))
                    aggregate = ordered[index]
            elif aggregation == Aggregation.MEDIAN.value:
                ordered = sorted(values)
                middle = len(ordered) // 2
                aggregate = (
                    ordered[middle]
                    if len(ordered) % 2
                    else (ordered[middle - 1] + ordered[middle]) / 2
                )
            else:
                ordered = sorted(values)
                if estimator == Estimator.TRIMMED_MEAN.value and len(ordered) >= 10:
                    trim = max(1, len(ordered) // 10)
                    ordered = ordered[trim:-trim]
                aggregate = sum(ordered) / len(ordered)
            return MeasurementSufficiency.SUFFICIENT, aggregate

        def constraint_passes(
            constraint: Mapping[str, Any],
            measurement: Mapping[str, Any],
            observed: float,
        ) -> bool:
            bound = constraint["bound"]
            sample_units = {
                str(sample["value"].get("unit"))
                for sample in samples
                if sample.get("counterId")
                == measurement.get("counter_id", measurement.get("counterId"))
            }
            bound_unit = str(bound.get("unit"))
            uncertainty = measurement.get(
                "uncertainty_rule", measurement.get("uncertaintyRule", {})
            )
            parameter = uncertainty.get("parameter", {})
            if sample_units != {bound_unit} or parameter.get("unit") != bound_unit:
                raise KernelRefusal("MEASUREMENT_UNIT_MISMATCH")
            bound_value = float(bound["value"])
            operator = constraint["operator"]
            margin = abs(float(parameter.get("value", 0)))
            direction = uncertainty.get(
                "conservative_direction",
                uncertainty.get("conservativeDirection", "two_sided"),
            )
            lower_observed = observed - (
                margin if direction in {"lower", "two_sided"} else 0
            )
            upper_observed = observed + (
                margin if direction in {"upper", "two_sided"} else 0
            )
            if operator == ComparisonOperator.GREATER_OR_EQUAL.value:
                return lower_observed >= bound_value
            if operator == ComparisonOperator.LESS_OR_EQUAL.value:
                return upper_observed <= bound_value
            if operator == ComparisonOperator.EQUAL.value:
                return lower_observed == upper_observed == bound_value
            if operator == ComparisonOperator.NOT_EQUAL.value:
                return bound_value < lower_observed or bound_value > upper_observed
            if operator == ComparisonOperator.MEMBER_OF.value:
                return str(observed) in set(
                    constraint.get("allowed_values", constraint.get("allowedValues", []))
                )
            raise KernelRefusal("UNKNOWN_COMPARISON_OPERATOR")

        for predicate in target.get("predicates", []):
            predicate_id = str(predicate.get("predicate_id", predicate.get("predicateId")))
            if bool(predicate.get("mandatory", True)):
                mandatory_predicate_ids.append(predicate_id)
            constraint = predicate["constraint"]
            measurement_ref = constraint.get(
                "measurement_ref", constraint.get("measurementRef")
            )
            measurement = measurements.get(str(measurement_ref))
            if measurement is None:
                raise KernelRefusal(
                    "MEASUREMENT_CONTRACT_NOT_RESOLVED", detail=str(measurement_ref)
                )
            measurement_hold = int(
                measurement.get("hold_ms", measurement.get("holdMs", 0))
            )
            hold_requirements.append(measurement_hold)
            window_indices = completed_window_indices(
                measurement, max(hold_requirements[0], measurement_hold)
            )
            window_results = [
                measured_value(measurement, index) for index in window_indices
            ]
            if not window_results:
                window_results = [
                    (MeasurementSufficiency.INSUFFICIENT_COVERAGE, None)
                ]
            predicate_sufficiencies = [item[0] for item in window_results]
            insufficiencies.extend(predicate_sufficiencies)
            if (
                any(
                    item is not MeasurementSufficiency.SUFFICIENT
                    for item in predicate_sufficiencies
                )
                or validity is not ExecutionValidity.VALID
            ):
                verdicts[predicate_id] = PredicateVerdict.INDETERMINATE
                if MeasurementSufficiency.MISSING_INTERVAL in predicate_sufficiencies:
                    already_charged = any(
                        entry.get("trialId") == trial_id
                        and entry.get("chargedForMissingInterval")
                        and entry.get("harmContractRef")
                        == state["cases"][trial["caseId"]]["harmContractRefs"][0]
                        for entry in self._state()["harmLedger"]
                    )
                    if not already_charged:
                        raw_charge = measurement.get(
                            "missing_interval_charge",
                            measurement.get("missingIntervalCharge"),
                        )
                        if raw_charge is None:
                            raise KernelRefusal("MISSING_INTERVAL_CHARGE_UNDEFINED")
                        if "source_record" in raw_charge:
                            charge = TypedQuantity(
                                value=raw_charge["value"],
                                unit=raw_charge["unit"],
                                provenance=Provenance(raw_charge["provenance"]),
                                source_record=raw_charge["source_record"],
                                document_status=DocumentStatus(
                                    raw_charge.get("document_status", "NORMATIVE")
                                ),
                                derivation_rule=raw_charge.get("derivation_rule"),
                                input_refs=tuple(raw_charge.get("input_refs", [])),
                            )
                        else:
                            charge = TypedQuantity.from_canonical_dict(raw_charge)
                        self.charge_harm(
                            trial_id,
                            amount=charge,
                            harm_kind=HarmKind.TRIAL_INDUCED,
                            for_missing_interval=True,
                            now=now,
                        )
                continue
            passed = all(
                observed is not None
                and constraint_passes(constraint, measurement, observed)
                for _, observed in window_results
            )
            verdicts[predicate_id] = (
                PredicateVerdict.PASS if passed else PredicateVerdict.FAIL
            )

        validity_region_stable = validity is ExecutionValidity.VALID
        for constraint in target.get(
            "validity_region", target.get("validityRegion", [])
        ):
            measurement_ref = constraint.get(
                "measurement_ref", constraint.get("measurementRef")
            )
            measurement = measurements.get(str(measurement_ref))
            if measurement is None:
                raise KernelRefusal(
                    "MEASUREMENT_CONTRACT_NOT_RESOLVED", detail=str(measurement_ref)
                )
            measurement_hold = int(
                measurement.get("hold_ms", measurement.get("holdMs", 0))
            )
            hold_requirements.append(measurement_hold)
            region_results = [
                measured_value(measurement, index)
                for index in completed_window_indices(
                    measurement, max(hold_requirements[0], measurement_hold)
                )
            ]
            if not region_results:
                region_results = [
                    (MeasurementSufficiency.INSUFFICIENT_COVERAGE, None)
                ]
            region_sufficiencies = [item[0] for item in region_results]
            insufficiencies.extend(region_sufficiencies)
            if (
                any(
                    item is not MeasurementSufficiency.SUFFICIENT
                    for item in region_sufficiencies
                )
                or not all(
                    observed is not None
                    and constraint_passes(constraint, measurement, observed)
                    for _, observed in region_results
                )
            ):
                validity = ExecutionValidity.INVALID
                validity_region_stable = False
                reasons = [StopReason.VALIDITY_EXIT]
                if stop_reason is not None:
                    reasons.append(StopReason(stop_reason))
                stop_reason = strongest_reason(reasons).value

        priority = (
            MeasurementSufficiency.MISSING_INTERVAL,
            MeasurementSufficiency.CLOCK_UNHEALTHY,
            MeasurementSufficiency.STALE,
            MeasurementSufficiency.SCOPE_MISMATCH,
            MeasurementSufficiency.INSUFFICIENT_COVERAGE,
        )
        sufficiency = next(
            (item for item in priority if item in insufficiencies),
            MeasurementSufficiency.SUFFICIENT,
        )
        required_hold = max(hold_requirements or [0])
        elapsed_hold = elapsed_observation_ms
        hold_complete = elapsed_hold >= required_hold and (
            required_hold == 0
            or sufficiency is MeasurementSufficiency.SUFFICIENT
        )
        active_candidate = frozen_candidate(state.get("catalog") or {}, trial["candidateId"])
        same_candidate = (
            active_candidate is not None
            and active_candidate.get("semanticHash")
            == trial.get("candidateSemanticHash")
            and candidate_availability(state, trial["candidateId"])
            == CandidateAvailability.IN_FLIGHT.value
        )
        self._append(
            "TrialEvaluated",
            object_id=trial_id,
            now=now,
            payload={
                "executionValidity": validity.value,
                "holdComplete": hold_complete,
                "mandatoryPredicateIds": sorted(mandatory_predicate_ids),
                "measurementSufficiency": sufficiency.value,
                "predicateVerdicts": {
                    key: value.value for key, value in sorted(verdicts.items())
                },
                "sameCandidate": same_candidate,
                "stopReason": stop_reason,
                "traceRefs": sorted(used_trace_refs),
                "trialId": trial_id,
                "validityRegionStable": validity_region_stable,
            },
        )
        return validity, sufficiency, verdicts

    def close_evidence(
        self, *, cell_id: str, contribution: Any, now: str
    ) -> EvidenceCellStatus:
        """Record a contribution and return the cell's resulting status.

        Signature frozen; body owned by lane **KERN**.

        Must apply :func:`assurance.core.axes.counts_toward_closure` before
        counting anything, must count *independent* contributions by
        dependency group rather than by row, must keep a dormant cell sealed
        until its vector is active, must keep a historical pass provisional
        until a full confirmation trial, and must append a compatible pass
        after a closed fail as a post-closure witness instead of reopening the
        cell.  All five rules are design section 8.
        """
        self._ensure_initialized()
        if not isinstance(contribution, EvidenceContribution):
            raise TypeError("contribution must be EvidenceContribution")
        state = self._state()
        try:
            cell = state["evidenceCells"][cell_id]
        except KeyError as exc:
            raise KernelRefusal("UNKNOWN_EVIDENCE_CELL", detail=cell_id) from exc
        if contribution.candidate_semantic_hash != cell["candidateSemanticHash"]:
            raise KernelRefusal("EVIDENCE_CANDIDATE_MISMATCH")

        if contribution.reused_from_epoch is not None:
            compatible = any(
                record.get("admitted")
                and record.get("sourceEpochRef") == contribution.reused_from_epoch
                and record.get("targetEpochRef") == state.get("activeEpoch")
                and record.get("candidateSemanticHash")
                == contribution.candidate_semantic_hash
                and set(record.get("results", {}))
                == {check.value for check in CompatibilityCheck}
                and all(record.get("results", {}).values())
                for record in state["compatibility"].values()
            )
            if not compatible:
                raise KernelRefusal("INCOMPATIBLE_EVIDENCE_REUSE")
        else:
            trial = state["trials"].get(contribution.trial_ref)
            if trial is None:
                raise KernelRefusal(
                    "EVIDENCE_TRIAL_NOT_FOUND", detail=contribution.trial_ref
                )
            if trial.get("candidateSemanticHash") != contribution.candidate_semantic_hash:
                raise KernelRefusal("EVIDENCE_TRIAL_CANDIDATE_MISMATCH")
            if (
                trial.get("caseId") != cell.get("caseId")
                or trial.get("epochId") != cell.get("epochId")
                or trial.get("targetRef") != cell.get("targetRef")
            ):
                raise KernelRefusal("EVIDENCE_TRIAL_SCOPE_MISMATCH")
            if trial.get("state") not in {
                TrialState.DECISION_HOLD.value,
                TrialState.FINALIZING_LIVE.value,
                TrialState.SETTLEMENT.value,
                TrialState.SETTLED_SUCCESS.value,
                TrialState.SETTLED_NON_SUCCESS.value,
            }:
                raise KernelRefusal("EVIDENCE_TRIAL_NOT_ELIGIBLE")
            evaluation = trial.get("evaluation", {})
            mandatory_ids = tuple(evaluation.get("mandatoryPredicateIds", []))
            mandatory_verdicts = [
                evaluation.get("predicateVerdicts", {}).get(predicate_id)
                for predicate_id in mandatory_ids
            ]
            if mandatory_verdicts and all(
                verdict == PredicateVerdict.PASS.value
                for verdict in mandatory_verdicts
            ):
                expected_verdict = PredicateVerdict.PASS.value
            elif any(
                verdict == PredicateVerdict.FAIL.value
                for verdict in mandatory_verdicts
            ):
                expected_verdict = PredicateVerdict.FAIL.value
            else:
                expected_verdict = PredicateVerdict.INDETERMINATE.value
            if (
                evaluation.get("executionValidity")
                != contribution.execution_validity.value
                or evaluation.get("measurementSufficiency")
                != contribution.measurement_sufficiency.value
                or contribution.predicate_verdict.value != expected_verdict
            ):
                raise KernelRefusal("EVIDENCE_EVALUATION_MISMATCH")
            observed_traces = {
                str(sample.get("traceHash"))
                for sample in state["samples"].values()
                if sample.get("traceHash") is not None
            }
            if not contribution.trace_refs or not set(contribution.trace_refs).issubset(
                observed_traces
            ):
                raise KernelRefusal("EVIDENCE_TRACE_NOT_FOUND")
            if not set(contribution.trace_refs).issubset(
                set(evaluation.get("traceRefs", []))
            ):
                raise KernelRefusal("EVIDENCE_TRACE_NOT_USED_BY_EVALUATION")

        previous_status = EvidenceCellStatus(
            cell.get("pendingStatus", cell["status"])
        )
        recorded = contribution
        resulting = previous_status
        if previous_status is EvidenceCellStatus.CLOSED_FAIL and (
            contribution.predicate_verdict is PredicateVerdict.PASS
        ):
            recorded = replace(contribution, is_post_closure_witness=True)
            resulting = EvidenceCellStatus.CLOSED_FAIL
        elif previous_status in {
            EvidenceCellStatus.CLOSED_PASS,
            EvidenceCellStatus.CLOSED_FAIL,
        }:
            recorded = replace(contribution, is_post_closure_witness=True)
        elif previous_status is EvidenceCellStatus.DORMANT_SEALED:
            resulting = EvidenceCellStatus.DORMANT_SEALED
        elif contribution.reused_from_epoch is not None:
            resulting = EvidenceCellStatus.PROVISIONAL_HISTORICAL
        else:
            existing = [
                *cell.get("contributions", []),
                *[
                    item["contribution"]
                    for item in cell.get("pendingEvidenceUpdates", [])
                ],
            ]
            new_payload = _contribution_payload(recorded)
            usable = [
                item
                for item in [*existing, new_payload]
                if item.get("reusedFromEpoch") is None
                and not item.get("isPostClosureWitness")
                and counts_toward_closure(
                    ExecutionValidity(item["executionValidity"]),
                    MeasurementSufficiency(item["measurementSufficiency"]),
                    PredicateVerdict(item["predicateVerdict"]),
                )
            ]
            independent: list[Mapping[str, Any]] = []
            groups: set[str] = set()
            traces: set[str] = set()
            for item in usable:
                group = item.get("dependencyGroup")
                item_traces = {str(trace) for trace in item.get("traceRefs", [])}
                if group is not None and str(group) in groups:
                    continue
                if item_traces & traces:
                    continue
                independent.append(item)
                if group is not None:
                    groups.add(str(group))
                traces.update(item_traces)
            quota = int(cell["requiredIndependentContributions"])
            if len(independent) >= quota:
                if all(
                    item["predicateVerdict"] == PredicateVerdict.PASS.value
                    for item in independent[:quota]
                ):
                    resulting = EvidenceCellStatus.CLOSED_PASS
                else:
                    resulting = EvidenceCellStatus.CLOSED_FAIL
            elif independent:
                resulting = EvidenceCellStatus.PARTIAL
            else:
                resulting = EvidenceCellStatus.OPEN

        self._append(
            "EvidenceContributionRecorded",
            object_id=cell_id,
            now=now,
            payload={
                "cellId": cell_id,
                "contribution": _contribution_payload(recorded),
                "previousStatus": previous_status.value,
                "resultingStatus": resulting.value,
            },
            idempotency_key=f"evidence:{cell_id}:{contribution.contribution_id}",
        )
        return resulting

    # ------------------------------------------------------------------ #
    # Exhaustion, release, termination (design sections 6.4, 8)
    # ------------------------------------------------------------------ #

    def exhaustion_certificate(self, *, vector_ref: str) -> Tuple[AggregateState, str]:
        """Assess the current vector's exhaustion; return state and evidence.

        Signature frozen; body owned by lane **KERN**.

        Returns :attr:`~assurance.core.axes.AggregateState.EXHAUSTED` with a
        certificate hash only when no obligation is ``OPEN``, ``PARTIAL``,
        ``TEMP_BLOCKED`` or ``BUDGET_LOCKED``; otherwise
        :attr:`~assurance.core.axes.AggregateState.EVIDENCE_INCOMPLETE`
        (design section 6.4, task section 6.16).  There is no relaxation path
        and no "sufficiently exhausted" threshold.
        """
        state = self._state()
        matching_cases = [
            case_id
            for case_id, case in state["cases"].items()
            if case.get("terminal") is None and case.get("activeVector") == vector_ref
        ]
        if not matching_cases:
            raise KernelRefusal("NO_ACTIVE_CASE_FOR_VECTOR")
        if len(matching_cases) != 1:
            raise KernelRefusal("AMBIGUOUS_ACTIVE_CASE_FOR_VECTOR")
        return self._exhaustion_for_case(
            case_id=matching_cases[0], vector_ref=vector_ref, state=state
        )

    def _exhaustion_for_case(
        self,
        *,
        case_id: str,
        vector_ref: str,
        state: Optional[Mapping[str, Any]] = None,
    ) -> Tuple[AggregateState, str]:
        reduced = state if state is not None else self._state()
        cells = [
            cell
            for cell in reduced["evidenceCells"].values()
            if cell.get("caseId") == case_id
            if cell.get("sealedUntilVectorRef") in {None, vector_ref}
        ]
        statuses = frozenset(EvidenceCellStatus(cell["status"]) for cell in cells)
        catalog = reduced.get("catalog") or {}
        lazy = bool(reduced["cases"].get(case_id, {}).get("lazyEvidenceCells"))
        if lazy and len(cells) < int(catalog.get("cardinality", 0)):
            # A frozen domain registers a cell only when its point is first
            # trialled (2026-09-19); every point not yet registered is an open
            # obligation, exactly as its pre-registered cell used to be.
            statuses = statuses | {EvidenceCellStatus.OPEN}
        non_closed_statuses = OPEN_EVIDENCE_CELL_STATUSES | {
            EvidenceCellStatus.DORMANT_SEALED,
            EvidenceCellStatus.PROVISIONAL_HISTORICAL,
        }
        if statuses & non_closed_statuses:
            aggregate = AggregateState.EVIDENCE_INCOMPLETE
        elif not statuses:
            aggregate = AggregateState.EVIDENCE_INCOMPLETE
        else:
            aggregate = aggregate_from_cells(
                statuses,
                any_deployed_success=bool(
                    reduced["cases"].get(case_id, {}).get("deployedSuccess")
                ),
            )
            if aggregate is AggregateState.IN_PROGRESS:
                aggregate = AggregateState.EVIDENCE_INCOMPLETE
        certificate_hash = content_hash(
            {
                "aggregate": aggregate.value,
                "caseId": case_id,
                "cells": {
                    cell["cellId"]: cell["status"]
                    for cell in sorted(cells, key=lambda item: item["cellId"])
                },
                "vectorRef": vector_ref,
            }
        )
        return aggregate, certificate_hash

    def release_target_vector(self, *, vector_ref: str, now: str) -> EventEnvelope:
        """Activate the next confirmed target vector.

        Signature frozen; body owned by lane **KERN**.

        Permitted only against a valid exhaustion certificate for the current
        vector, and only in the Operator-confirmed order.  Unseals the newly
        active vector's dormant evidence.
        """
        state = self._state()
        case_matches = [
            (case_id, case)
            for case_id, case in state["cases"].items()
            if case.get("terminal") is None
        ]
        if not case_matches:
            raise KernelRefusal("NO_ACTIVE_CASE")
        if len(case_matches) != 1:
            raise KernelRefusal("AMBIGUOUS_ACTIVE_CASE")
        case_id, case = case_matches[0]
        order = state["epochs"][state["activeEpoch"]].get("targetVectorOrder", [])
        current = case.get("activeVector")
        try:
            next_index = order.index(current) + 1
        except (ValueError, AttributeError) as exc:
            raise KernelRefusal("ACTIVE_VECTOR_NOT_FROZEN") from exc
        if next_index >= len(order) or order[next_index] != vector_ref:
            raise KernelRefusal("TARGET_VECTOR_ORDER_VIOLATION")
        aggregate, certificate_hash = self._exhaustion_for_case(
            case_id=case_id, vector_ref=current, state=state
        )
        if aggregate is not AggregateState.EXHAUSTED:
            raise KernelRefusal("CURRENT_VECTOR_NOT_EXHAUSTED", detail=aggregate.value)
        self._append(
            "ExhaustionCertified",
            object_id=f"{case_id}:{current}",
            now=now,
            payload={
                "aggregate": aggregate.value,
                "caseId": case_id,
                "certificateHash": certificate_hash,
                "vectorRef": current,
            },
            idempotency_key=f"exhaustion:{current}:{certificate_hash}",
        )
        unsealed: Dict[str, str] = {}
        for cell_id, cell in state["evidenceCells"].items():
            if (
                cell.get("caseId") != case_id
                or cell.get("sealedUntilVectorRef") != vector_ref
            ):
                continue
            usable = [
                item
                for item in cell.get("contributions", [])
                if item.get("reusedFromEpoch") is None
                and not item.get("isPostClosureWitness")
            ]
            unsealed[cell_id] = (
                EvidenceCellStatus.PARTIAL.value
                if usable
                else EvidenceCellStatus.OPEN.value
            )
        return self._append(
            "TargetVectorReleased",
            object_id=case_id,
            now=now,
            payload={
                "caseId": case_id,
                "exhaustionCertificateHash": certificate_hash,
                "previousVectorRef": current,
                "unsealedCells": unsealed,
                "vectorRef": vector_ref,
            },
        )

    def terminate_case(self, *, case_id: str, now: str) -> CaseTermination:
        """Terminate a case through one of the six finite endings.

        Signature frozen; body owned by lane **KERN**.

        Design section 8: success, all confirmed vectors exhausted, evidence
        incomplete, operator abort, safety incident, or recovery failure.  "No
        agent can keep a case alive past its deadline, trial cap, proposal cap,
        or usable harm reserve" -- so this method must be reachable from the
        case policy's own numbers without any agent participating.
        """
        state = self._state()
        try:
            case = state["cases"][case_id]
        except KeyError as exc:
            raise KernelRefusal("UNKNOWN_CASE", detail=case_id) from exc
        if case.get("terminal") is not None:
            return CaseTermination(case["terminal"])
        trials = [
            trial for trial in state["trials"].values() if trial["caseId"] == case_id
        ]
        if any(
            trial["state"] == TrialState.INCIDENT_LOCKDOWN.value
            or trial["outcome"] == TrialOutcome.RECOVERY_FAILED.value
            for trial in trials
        ):
            termination = CaseTermination.RECOVERY_FAILURE
        elif any(
            trial.get("stopReason") == StopReason.OPERATOR_ABORT.value for trial in trials
        ):
            termination = CaseTermination.OPERATOR_ABORT
        elif any(
            trial.get("stopReason")
            in {
                StopReason.HARM_LIMIT_BREACH.value,
                StopReason.HARD_SAFETY_GUARD.value,
            }
            for trial in trials
        ):
            termination = CaseTermination.SAFETY_INCIDENT
        elif case.get("deployedSuccess"):
            termination = CaseTermination.SUCCESS
        else:
            active_vector = case.get("activeVector")
            aggregate, _ = self._exhaustion_for_case(
                case_id=case_id, vector_ref=active_vector, state=state
            )
            order = state["epochs"][state["activeEpoch"]].get("targetVectorOrder", [])
            at_last_vector = bool(order) and active_vector == order[-1]
            if aggregate is AggregateState.EXHAUSTED and at_last_vector:
                termination = CaseTermination.VECTORS_EXHAUSTED
            else:
                bounds_spent = (
                    parse_utc(now) >= parse_utc(case["deadlineAt"])
                    or len(trials) >= int(case["maxTrials"])
                    or (self._remaining_reserve(case_id, state=state) or 0) <= 0
                )
                proposals = sum(
                    1
                    for envelope in self._event_store.iterate()
                    if envelope.event_kind
                    in {"AdvisoryAccepted", "AdvisoryRejected"}
                    and envelope.payload.get("correlationId") == case_id
                )
                bounds_spent = bounds_spent or proposals >= int(case["maxProposals"])
                if not bounds_spent:
                    raise KernelRefusal("CASE_NOT_TERMINABLE")
                termination = CaseTermination.EVIDENCE_INCOMPLETE
        self._append(
            "CaseTerminated",
            object_id=case_id,
            now=now,
            payload={"caseId": case_id, "termination": termination.value},
            idempotency_key=f"case-terminal:{case_id}",
        )
        return termination

    # ------------------------------------------------------------------ #
    # Settlement and recovery (design sections 7 step 10, 8)
    # ------------------------------------------------------------------ #

    def settle_trial(
        self, trial_id: str, *, outcome: TrialOutcome, now: str
    ) -> EventEnvelope:
        """Write the single idempotent settlement event.

        Signature frozen; body owned by lane **KERN**.

        One event updates outcome, harm charge, reserve return, evidence,
        locks, deployment state and case state together (design section 7 step
        10, task section 6.11).  One event, not seven: a crash between two of
        them would leave a charged reserve with no recorded outcome, or a
        released lock on a still-applied configuration.

        Idempotent by the trial's settlement idempotency key.  Calling it
        twice appends nothing the second time and returns the original
        envelope, so a retried settlement after a lost ack cannot double-charge.

        A ``SUCCESS`` outcome is permitted only after live finalization: durable
        success decision, configuration reread and finalize acknowledgement
        (task section 6.9).  Every post-commit non-success must already have
        passed through reverse rollback and recovery reread (task 6.10).
        """
        self._ensure_initialized()
        if not isinstance(outcome, TrialOutcome) or outcome is TrialOutcome.NOT_SETTLED:
            raise KernelRefusal("INVALID_SETTLEMENT_OUTCOME")
        idempotency_key = f"settlement:{trial_id}"
        for envelope in self._event_store.iterate():
            if envelope.idempotency_key != idempotency_key:
                continue
            if envelope.payload.get("outcome") != outcome.value:
                raise KernelRefusal("IDEMPOTENCY_COLLISION")
            return envelope
        state = self._state()
        try:
            trial = state["trials"][trial_id]
        except KeyError as exc:
            raise KernelRefusal("UNKNOWN_TRIAL", detail=trial_id) from exc
        if trial["state"] != TrialState.SETTLEMENT.value:
            raise KernelRefusal("SETTLEMENT_STATE_REQUIRED")
        if outcome is TrialOutcome.SUCCESS:
            if (
                not trial.get("successDecisionDurable")
                or not trial.get("configurationReread")
                or not trial.get("finalizeAcknowledged")
            ):
                raise KernelRefusal("LIVE_FINALIZE_INCOMPLETE")
        elif trial.get("applyCounted") and not trial.get("recoveryVerified"):
            raise KernelRefusal("POST_COMMIT_RECOVERY_REQUIRED")

        reservations = [
            entry
            for entry in state["harmLedger"]
            if entry.get("trialId") == trial_id
            and entry.get("movementKind") == "RESERVE"
        ]
        charges = [
            entry
            for entry in state["pendingHarmCharges"].get(trial_id, [])
        ]
        harm_movements = []
        for reservation in reservations:
            charged = sum(
                float(entry["amount"]["value"])
                for entry in charges
                if entry.get("harmContractRef") == reservation.get("harmContractRef")
                and entry.get("harmKind") == reservation.get("harmKind")
                and entry.get("amount", {}).get("unit")
                == reservation.get("amount", {}).get("unit")
            )
            returned = float(reservation["amount"]["value"]) - charged
            if returned <= 0:
                continue
            return_amount = dict(reservation["amount"])
            return_amount["value"] = returned
            harm_movements.append(
                {
                    "amount": return_amount,
                    "caseId": trial["caseId"],
                    "chargedForMissingInterval": False,
                    "epochId": trial["epochId"],
                    "harmContractRef": reservation["harmContractRef"],
                    "harmKind": reservation["harmKind"],
                    "movementKind": "RETURN",
                    "reason": "atomic_trial_settlement",
                }
            )
        evidence_updates = []
        for cell_id, cell in sorted(state["evidenceCells"].items()):
            trial_updates = [
                item
                for item in cell.get("pendingEvidenceUpdates", [])
                if item.get("contribution", {}).get("trialRef") == trial_id
            ]
            if trial_updates:
                evidence_updates.append(
                    {
                        "cellId": cell_id,
                        "contributions": [
                            item["contribution"] for item in trial_updates
                        ],
                        "resultingStatus": trial_updates[-1]["resultingStatus"],
                    }
                )
        return self._append(
            "TrialSettled",
            object_id=trial_id,
            now=now,
            payload={
                "candidateId": trial["candidateId"],
                "caseId": trial["caseId"],
                "deploymentDisposition": (
                    "FINALIZED_LIVE"
                    if outcome is TrialOutcome.SUCCESS
                    else "RECOVERED_OR_PRE_COMMIT"
                ),
                "evidenceUpdates": evidence_updates,
                "harmCharges": [
                    {
                        key: value
                        for key, value in entry.items()
                        if key not in {"eventId", "timestamp"}
                    }
                    for entry in charges
                ],
                "harmMovements": harm_movements,
                "locksReleased": [
                    resource_id
                    for resource_id, owner in state["resourceLocks"].items()
                    if owner == trial_id
                ],
                "outcome": outcome.value,
                "transactionId": trial["transactionId"],
                "trialId": trial_id,
            },
            idempotency_key=idempotency_key,
        )

    def recover(self, *, now: str) -> Sequence[str]:
        """Resolve every uncertain transaction after a restart.

        Signature frozen; body owned by lane **KERN**.

        Returns the transaction ids still unresolved after the pass; an empty
        return is what unblocks new trials.

        For each uncertain transaction the Kernel queries the Write Gateway
        and drives it to exactly one of the four safe resolutions in design
        section 8: safely aborted, finalized, rolled back, or placed in
        incident lockdown.  "Assume it did not apply" is not one of them --
        the partial-apply and lost-ack cases are precisely why the query
        exists.
        """
        self._ensure_initialized()
        self._recovery_spent_ms = 0
        try:
            return self._recover_pass(now)
        finally:
            self._recovery_spent_ms = None

    def _reverse_own_then_lock_down(self, trial_id: str, transaction_id: str,
                                    resource_id: str, *, now: str) -> None:
        """A readable configuration outside our plan still locks the trial down, but the
        policies *we* created are withdrawn first (2026-09-26, board 670 trial 7: a UE
        re-registered mid-commit, its reset weight read as foreign, and the lockdown left
        our cap, weight and cell-power policies at the RIC -- gnb2 stayed at 9 dB).  The
        reversal touches only the axes this transaction applied and returns them to the
        plan's baseline -- exactly what an ordinary rollback does; its outcome is recorded
        and the resolution is INCIDENT_LOCKDOWN whatever it says.

        Known limit (Codex review 2026-09-26): for a non-sentinel baseline the R1 adapter
        restores by UPDATE, so an applied axis that a *different writer* has since changed
        would be overwritten with our baseline.  On this testbed the Kernel is the only
        writer and a foreign value comes from a UE context reset or a retained value, so the
        withdrawal is the safer direction; with several writers this needs per-axis
        ownership before it may run."""
        try:
            current = TrialState(self._state()["trials"][trial_id]["state"])
            if current is TrialState.STOPPING:
                self.advance_trial(trial_id, TrialState.REVERSE_ROLLBACK, now=now)
            elif current is TrialState.RECOVERY_VERIFYING:
                self.advance_trial(trial_id, TrialState.REVERSE_ROLLBACK, now=now)
            if TrialState(self._state()["trials"][trial_id]["state"]) is TrialState.REVERSE_ROLLBACK:
                token = self.issue_token(trial_id, token_kind=TokenKind.REVERSE_ROLLBACK, now=now)
                result = self._write_gateway.reverse_rollback(token=token)
                if isinstance(result, GatewayResult):
                    self.record_gateway_result(token, result, resource_id=resource_id, now=now)
        except Exception:  # noqa: BLE001 - Codex review 2026-09-26: a journal or gateway
            # failure in this best-effort withdrawal must not keep the lockdown below from
            # being recorded, nor stop the pass from reaching other uncertain transactions.
            # If the event store itself is broken, resolve_recovery raises and the pass
            # stops -- fail closed.
            pass
        self.resolve_recovery(transaction_id, resolution="INCIDENT_LOCKDOWN", now=now)

    def _recover_pass(self, now: str) -> Sequence[str]:
        """The body of :meth:`recover`; permits it issues see ``_lease_ms``."""
        uncertain = tuple(self._event_store.uncertain_transactions())
        for transaction_id in uncertain:
            self._append(
                "RecoveryStarted",
                object_id=transaction_id,
                now=now,
                payload={"transactionId": transaction_id},
            )
            if self._write_gateway is None:
                continue
            # The query is a gateway call too; permits issued after it cover it.
            self._recovery_spent_ms += DEFAULT_LEASE_MS
            result = self._write_gateway.query_transaction(transaction_id)
            if not isinstance(result, GatewayResult):
                raise KernelRefusal("INVALID_RECOVERY_QUERY_RESULT")
            self._append(
                "RecoveryObserved",
                object_id=transaction_id,
                now=now,
                payload={
                    "observedConfigHash": result.observed_config_hash,
                    "outcome": result.outcome.value,
                    "transactionId": transaction_id,
                },
            )
            state = self._state()
            transaction = state["transactions"].get(transaction_id, {})
            trial_id = transaction.get("trialId")
            if trial_id not in state["trials"]:
                continue
            current = TrialState(state["trials"][trial_id]["state"])
            applied = result.outcome in {
                GatewayOutcome.ACKED,
                GatewayOutcome.ALREADY_APPLIED,
                GatewayOutcome.PARTIAL_APPLY,
            }
            safely_absent = result.outcome in {
                GatewayOutcome.REJECTED,
                GatewayOutcome.REJECTED_CONFIG_MISMATCH,
                GatewayOutcome.REJECTED_FENCE,
                GatewayOutcome.REJECTED_LEASE_EXPIRED,
            }
            resource_id = state["trials"][trial_id]["resourceId"]

            if current is TrialState.FINALIZING_LIVE and applied:
                reread_token = self.issue_token(
                    trial_id, token_kind=TokenKind.CONFIGURATION_REREAD, now=now
                )
                reread_result = self._write_gateway.reread_configuration(
                    token=reread_token
                )
                if not isinstance(reread_result, GatewayResult):
                    raise KernelRefusal("INVALID_RECOVERY_ACTION_RESULT")
                self.record_gateway_result(
                    reread_token, reread_result, resource_id=resource_id, now=now
                )
                if self._state()["trials"][trial_id].get("configurationReread"):
                    finalize_token = self.issue_token(
                        trial_id, token_kind=TokenKind.FINALIZE_LIVE, now=now
                    )
                    finalize_result = self._write_gateway.finalize_live(
                        token=finalize_token
                    )
                    if not isinstance(finalize_result, GatewayResult):
                        raise KernelRefusal("INVALID_RECOVERY_ACTION_RESULT")
                    self.record_gateway_result(
                        finalize_token,
                        finalize_result,
                        resource_id=resource_id,
                        now=now,
                    )
                    if self._state()["trials"][trial_id].get(
                        "finalizeAcknowledged"
                    ):
                        self.resolve_recovery(
                            transaction_id, resolution="FINALIZED", now=now
                        )
                        continue

            current = TrialState(self._state()["trials"][trial_id]["state"])

            if current not in {
                TrialState.STOPPING,
                TrialState.REVERSE_ROLLBACK,
                TrialState.RECOVERY_VERIFYING,
            }:
                if TrialState.RECOVERY_VERIFYING in self._successors(current):
                    self.advance_trial(
                        trial_id, TrialState.RECOVERY_VERIFYING, now=now
                    )
                elif TrialState.STOPPING in self._successors(current):
                    self.advance_trial(
                        trial_id,
                        TrialState.STOPPING,
                        reason=StopReason.PARTIAL_APPLY,
                        now=now,
                    )
                else:
                    continue

            current = TrialState(self._state()["trials"][trial_id]["state"])
            reread_result = None
            if not (applied or safely_absent) and current is TrialState.RECOVERY_VERIFYING:
                probe_token = self.issue_token(
                    trial_id, token_kind=TokenKind.CONFIGURATION_REREAD, now=now
                )
                reread_result = self._write_gateway.reread_configuration(
                    token=probe_token
                )
                if not isinstance(reread_result, GatewayResult):
                    raise KernelRefusal("INVALID_RECOVERY_ACTION_RESULT")
                self.record_gateway_result(
                    probe_token, reread_result, resource_id=resource_id, now=now
                )
                trial = self._state()["trials"][trial_id]
                # The staged plan's prefix digests are what an observed
                # configuration is classified against: index 0 is the
                # baseline (nothing landed), any later index is a change that
                # is live in whole or in part and owes a reverse rollback.
                # Comparing against the candidate's *semantic* hash instead
                # could never match a configuration digest, so every
                # resolvable transaction would have been locked down.
                prefixes = list(trial.get("planPrefixHashes") or [])
                observed_config = reread_result.observed_config_hash
                readable = reread_result.outcome in {
                    GatewayOutcome.ACKED,
                    GatewayOutcome.ALREADY_APPLIED,
                } and observed_config is not None
                if readable and observed_config == (
                    prefixes[0] if prefixes else trial.get("baselineHash")
                ):
                    safely_absent = True
                elif readable and (observed_config in prefixes[1:] or observed_config
                                   in _plan_subset_hashes(self._event_store, trial_id)):
                    # A prefix, or any other combination of our own staged
                    # values: A1 effects land out of order (see subset_hashes).
                    applied = True
                elif not readable:
                    # 2026-09-23 audit: the same rule as the sibling branch below.
                    # Unreadable is uncertain, not foreign: reverse the whole plan
                    # and let the confirming read decide.  Nothing counts as
                    # restored until that read says so.
                    applied = True
                else:
                    # Readable and foreign: a reversal cannot restore from it, so the
                    # trial locks down -- but our own policies are withdrawn first.
                    self._reverse_own_then_lock_down(
                        trial_id, transaction_id, resource_id, now=now)
                    continue

            rollback_required = applied or current in {
                TrialState.STOPPING,
                TrialState.REVERSE_ROLLBACK,
            }
            if rollback_required:
                rollback_token = None
                if reread_result is None:
                    # The permit about to be issued names a configuration the
                    # gateway must observe *before* reversing.  Nothing has
                    # been read back since the transaction became uncertain,
                    # so read it now rather than reversing against a belief.
                    refresh_token = self.issue_token(
                        trial_id, token_kind=TokenKind.CONFIGURATION_REREAD, now=now
                    )
                    reread_result = self._write_gateway.reread_configuration(
                        token=refresh_token
                    )
                    if not isinstance(reread_result, GatewayResult):
                        raise KernelRefusal("INVALID_RECOVERY_ACTION_RESULT")
                    self.record_gateway_result(
                        refresh_token, reread_result, resource_id=resource_id, now=now
                    )
                    prefixes = list(
                        self._state()["trials"][trial_id].get("planPrefixHashes") or []
                    )
                    readable_reread = reread_result.outcome in {
                        GatewayOutcome.ACKED,
                        GatewayOutcome.ALREADY_APPLIED,
                    } and reread_result.observed_config_hash is not None
                    # 2026-09-23 (live board 20260923T083611 trial 3): two ways
                    # this branch locked a board down *without reversing*,
                    # leaving our own policies at the RIC.
                    # (1) An own partial apply that is not a prefix -- A1 effects
                    #     land out of order -- was classed as foreign.
                    # (2) An *unreadable* reread (25 ms after the commit read)
                    #     was treated like a foreign configuration.  Unreadable is
                    #     uncertain, and the gateway already knows the direction
                    #     for uncertain: reverse the whole plan, then let the
                    #     confirming read decide.  Nothing here counts as
                    #     restored until that read says so.
                    # A configuration that is **readable and foreign** -- some
                    # axis at a value that is neither its baseline nor ours --
                    # still locks down: a reversal cannot restore from it.
                    foreign = readable_reread and bool(prefixes) and (
                        reread_result.observed_config_hash not in prefixes
                        and reread_result.observed_config_hash
                        not in _plan_subset_hashes(self._event_store, trial_id))
                    if foreign:
                        self._reverse_own_then_lock_down(
                            trial_id, transaction_id, resource_id, now=now)
                        continue
                current = TrialState(self._state()["trials"][trial_id]["state"])
                if current is TrialState.STOPPING:
                    self.advance_trial(
                        trial_id, TrialState.REVERSE_ROLLBACK, now=now
                    )
                elif current is TrialState.RECOVERY_VERIFYING:
                    rollback_token = self.issue_token(
                        trial_id, token_kind=TokenKind.REVERSE_ROLLBACK, now=now
                    )
                    self.advance_trial(
                        trial_id, TrialState.REVERSE_ROLLBACK, now=now
                    )
                current = TrialState(self._state()["trials"][trial_id]["state"])
                if current is TrialState.REVERSE_ROLLBACK and rollback_token is None:
                    rollback_token = self.issue_token(
                        trial_id, token_kind=TokenKind.REVERSE_ROLLBACK, now=now
                    )
                rollback_result = self._write_gateway.reverse_rollback(
                    token=rollback_token
                )
                if not isinstance(rollback_result, GatewayResult):
                    raise KernelRefusal("INVALID_RECOVERY_ACTION_RESULT")
                self.record_gateway_result(
                    rollback_token,
                    rollback_result,
                    resource_id=resource_id,
                    now=now,
                )
                if rollback_result.outcome not in {
                    GatewayOutcome.ACKED,
                    GatewayOutcome.ALREADY_APPLIED,
                }:
                    self.resolve_recovery(
                        transaction_id, resolution="INCIDENT_LOCKDOWN", now=now
                    )
                    continue
                self.advance_trial(
                    trial_id, TrialState.RECOVERY_VERIFYING, now=now
                )
                reread_result = None

            if reread_result is None or not self._state()["trials"][trial_id].get(
                "recoveryConfigurationReread"
            ):
                reread_token = self.issue_token(
                    trial_id, token_kind=TokenKind.CONFIGURATION_REREAD, now=now
                )
                reread_result = self._write_gateway.reread_configuration(
                    token=reread_token
                )
                if not isinstance(reread_result, GatewayResult):
                    raise KernelRefusal("INVALID_RECOVERY_ACTION_RESULT")
                self.record_gateway_result(
                    reread_token, reread_result, resource_id=resource_id, now=now
                )
            if not self._state()["trials"][trial_id].get(
                "recoveryConfigurationReread"
            ):
                self.resolve_recovery(
                    transaction_id, resolution="INCIDENT_LOCKDOWN", now=now
                )
                continue
            confirmation_token = self.issue_token(
                trial_id, token_kind=TokenKind.RECOVERY_CONFIRM, now=now
            )
            confirmation_result = self._write_gateway.confirm_recovery(
                token=confirmation_token
            )
            if not isinstance(confirmation_result, GatewayResult):
                raise KernelRefusal("INVALID_RECOVERY_ACTION_RESULT")
            self.record_gateway_result(
                confirmation_token,
                confirmation_result,
                resource_id=resource_id,
                now=now,
            )
            if not self._state()["trials"][trial_id].get("recoveryVerified"):
                self.resolve_recovery(
                    transaction_id, resolution="INCIDENT_LOCKDOWN", now=now
                )
                continue
            self.resolve_recovery(
                transaction_id,
                resolution="ROLLED_BACK" if rollback_required else "ABORTED",
                now=now,
            )
        return tuple(self._event_store.uncertain_transactions())

    def resolve_recovery(
        self, transaction_id: str, *, resolution: str, now: str
    ) -> EventEnvelope:
        """Record one of the four permitted uncertain-transaction resolutions."""
        permitted = {"ABORTED", "FINALIZED", "ROLLED_BACK", "INCIDENT_LOCKDOWN"}
        if resolution not in permitted:
            raise KernelRefusal("UNSAFE_RECOVERY_RESOLUTION")
        if transaction_id not in self._event_store.uncertain_transactions():
            raise KernelRefusal("TRANSACTION_NOT_UNCERTAIN")
        trial_id = None
        state = self._state()
        for trial in state["trials"].values():
            if trial.get("transactionId") == transaction_id:
                trial_id = trial["trialId"]
                break
        if trial_id is None:
            raise KernelRefusal("RECOVERY_TRIAL_NOT_FOUND")
        trial = state["trials"][trial_id]
        current = TrialState(trial["state"])
        if resolution == "INCIDENT_LOCKDOWN":
            if TrialState.INCIDENT_LOCKDOWN not in self._successors(current):
                raise KernelRefusal(
                    "INCIDENT_LOCKDOWN_TRANSITION_REQUIRED",
                    detail=current.value,
                )
            self.advance_trial(trial_id, TrialState.INCIDENT_LOCKDOWN, now=now)
        elif resolution == "FINALIZED":
            live_verified = (
                current is TrialState.FINALIZING_LIVE
                and trial.get("configurationReread")
                and trial.get("finalizeAcknowledged")
            )
            recovery_verified = (
                current is TrialState.RECOVERY_VERIFYING
                and trial.get("recoveryVerified")
            )
            if not (live_verified or recovery_verified):
                raise KernelRefusal("RECOVERY_NOT_VERIFIED")
        elif current is not TrialState.RECOVERY_VERIFYING or not trial.get(
            "recoveryVerified"
        ):
            raise KernelRefusal("RECOVERY_NOT_VERIFIED")
        return self._append(
            "TransactionResolved",
            object_id=transaction_id,
            now=now,
            payload={
                "resolution": resolution,
                "transactionId": transaction_id,
                "trialId": trial_id,
            },
            idempotency_key=f"recovery-resolution:{transaction_id}",
        )

    def terminal_state_hash(self) -> str:
        """The terminal state hash of the current stream.

        Signature frozen; body owned by lane **KERN**.

        Delegates to :func:`assurance.kernel.reducer.terminal_state_hash` over
        a full replay, so the value a live run reports and the value a replay
        of its stream reports are produced by the same code path.  Design
        section 15 compares them.
        """
        return hash_terminal_state(
            self._state(), reducer_version=self._reducer.reducer_version
        )
