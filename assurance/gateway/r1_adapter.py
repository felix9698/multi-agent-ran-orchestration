"""The R1 adapter skeleton: gateway commands onto the official O-RAN path.

Owner lane: **KGW**.

Design section 9 fixes the one path an objective effect may take::

    Assurance Kernel -> Write Gateway -> R1 -> Non-RT RIC -> A1-P -> xApp
    -> FlexRIC -> E2SM -> OAI gNB

This adapter is the first hop: it turns the gateway's closed command vocabulary
into calls on the project's existing R1 consumer (``oran/rapp/r1_client.py``)
and its policy translator (``oran/rapp/policy_translator.py``).

**Both are injected, never imported.**  ``assurance/**`` may not import
``oran.rapp`` -- the seam test enforces it, because a package that can reach the
transport is a package that can be made to open one.  So the adapter takes
*ports*: an object shaped like ``R1Client`` and a callable shaped like a
``translate_intent`` binding.  Gate 3 wires the real ones; Gate 2 injects a
fake transport and asserts the request shapes, which is the mapping test the
seam allows and the live call it forbids.

**An acknowledgement is not an effect.**  Task section 7.6 and design section 9
both refuse to let an A1 create, an E2 ACK or a command acceptance stand in for
success; the frozen ``AIC_UECellSteering_1.0.0`` status object says so in its
own fields (``control.resultIsEffectEvidence``, ``control.writeMayHaveOccurred``,
``readback.result``).  So :class:`R1Adapter` reads the configuration back
through :attr:`readback_port` -- the contracted effect readback -- and answers
``UNKNOWN`` when it has none.  It never derives a configuration digest from an
acceptance.

**The binding comes from the token.**  A policy id is recorded against
``token.transaction_id`` when it is created and looked up the same way for
update, rollback and halt.  Nothing in a command names a policy id, so a
proposal cannot address one -- GATE1-MAP's GAP-01 -- and the next trial cannot
inherit a scope the previous one never released -- GAP-04.

**A transport failure on a write is ``UNKNOWN``, not an error.**  The request
may have reached the Non-RT RIC; reporting a clean failure would tell the
Kernel nothing happened when something may have.  Read-only operations report
``ERROR``, because a read that failed changed nothing by definition.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, Tuple

from assurance.contracts.capability import ActuatorPath
from assurance.gateway.commands import GatewayOperation
from assurance.gateway.plan import config_hash
from assurance.gateway.power_spacing import POWER_POLICY_TYPE, power_write_wait_s
from assurance.gateway.r1_binding_journal import BindingState, R1BindingRecord
from assurance.gateway.r1_operation_journal import (
    InMemoryR1OperationJournal, R1Operation, R1OperationOutcome,
    R1PolicyOperation, write_counts,
)
from assurance.gateway.token import KernelToken
from assurance.gateway.write_gateway import GatewayOutcome, GatewayResult

__all__ = [
    "R1Adapter",
    "R1PolicyPort",
    "R1ReadbackPort",
    "project_verified_readback",
]


class R1PolicyPort(Protocol):
    """The subset of the project's R1 consumer this adapter uses.

    Named as a protocol so the dependency is a shape, not an import: the real
    ``oran.rapp.r1_client.R1Client`` satisfies it, and so does a test double.
    """

    def get_policy_type(self, policy_type_id: str) -> Mapping[str, Any]: ...

    def create_policy(
        self,
        near_rt_ric_id: str,
        policy_type_id: str,
        policy_object: Dict[str, Any],
    ) -> Mapping[str, Any]: ...

    def update_policy(
        self, policy_id: str, policy_object: Dict[str, Any]
    ) -> Mapping[str, Any]: ...

    def delete_policy(self, policy_id: str) -> None: ...

    def get_policy_status(self, policy_id: str) -> Mapping[str, Any]: ...


class R1ReadbackPort(Protocol):
    """The contracted effect readback, called with everything it can need.

    Keyword-only, and a protocol rather than a bare ``Callable``, because the
    two moments a readback is taken need different material and both are
    legitimate:

    ``prepare`` / pre-commit
        No policy exists yet, so *policy_id* is ``None``.  The observation has
        to come from somewhere that does not depend on this transaction --
        for ``PIN_TO_CELL``, the O1/KPM view of the UE's current serving cell.

    ``commit`` / ``finalize`` / rollback verification
        A policy is bound to the transaction, so the contracted readback is
        the one the frozen status object carries
        (``aicStatus.readback.observedServingCell`` when
        ``readback.result == "VERIFIED"``), and reaching it needs the policy
        id.  Passing *transaction_id* as well keeps the correlation the
        Kernel fences on visible to whatever answers.

    Returning ``None`` is the honest answer when there is no observation, and
    the gateway turns it into ``UNKNOWN`` rather than a digest.  A future
    field is added here with a default rather than by widening a positional
    signature again.
    """

    def __call__(
        self,
        *,
        scope: Mapping[str, Any],
        transaction_id: str,
        policy_id: Optional[str],
    ) -> Optional[Mapping[str, Any]]: ...


def _same(value: Any) -> str:
    """One spelling for a configuration value, whatever its key order."""
    return json.dumps(value, sort_keys=True, default=str)


def project_verified_readback(status: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """Project the contracted configuration surface out of a policy status.

    Returns ``None`` unless the status carries a *verified readback*.  The
    frozen status object distinguishes an acknowledgement
    (``control.result == "ACK"``, ``control.resultIsEffectEvidence == false``)
    from an observed effect (``readback.result == "VERIFIED"``), and only the
    second is admissible as the configuration this adapter reports.
    """
    aic = status.get("aicStatus") if isinstance(status, Mapping) else None
    if not isinstance(aic, Mapping):
        return None
    readback = aic.get("readback")
    if not isinstance(readback, Mapping) or readback.get("result") != "VERIFIED":
        return None
    observed = readback.get("observedServingCell")
    if observed is None:
        return None
    return {"servingCell": observed}


def _trace_of(policy: Any) -> Mapping[str, Any]:
    """The ``trace`` of a policy, whether it arrives bare or in an envelope.

    A PUT sends the bare body (``{config, validity, trace}``), but the R1 GET
    answers with the A1 envelope and puts that body under ``policyObject``::

        {"nearRtRicId": ..., "policyTypeId": ...,
         "policyObject": {"config": ..., "trace": {"fencingToken": 17, ...}}}

    Read live against the producer on 2026-09-17 before this path was trusted.
    Reading only the top level returned ``None`` for every policy, which would
    have left the fence seed inert on the bed while every hermetic test stayed
    green -- the same shape of mistake as a builder wrapper that swallows a
    keyword, found the same way: by going and looking at the real answer.
    """
    if not isinstance(policy, Mapping):
        return {}
    for candidate in (policy, policy.get("policyObject")):
        if isinstance(candidate, Mapping):
            trace = candidate.get("trace")
            if isinstance(trace, Mapping):
                return trace
    return {}


def _accepts_keyword(builder: Any, name: str) -> bool:
    """Whether *builder* takes *name* as a keyword.

    Inspected once, at construction, rather than discovered by catching a
    ``TypeError`` per draft: a ``TypeError`` raised *inside* a builder would
    then be misread as "this builder takes the older parameter list" and the
    seed would be silently dropped, which is the exact failure the seed exists
    to prevent.
    """
    import inspect

    try:
        spec = inspect.getfullargspec(builder)
    except TypeError:  # a builtin or a C callable
        return False
    if spec.varkw is not None:
        return True
    return name in spec.args or name in spec.kwonlyargs


def _accepts_last_fencing_token(builder: Any) -> bool:
    """True when the builder takes the producer's own fencingToken as a seed."""
    return _accepts_keyword(builder, "last_fencing_token")


def _accepts_last_revision(builder: Any) -> bool:
    """Whether *builder* takes the durable revision seed."""
    return _accepts_keyword(builder, "last_revision")


def _policy_id_of(result: Any) -> Optional[str]:
    """The policy id a create returned, when the port names one."""
    if isinstance(result, Mapping):
        value = result.get("policyId")
        if isinstance(value, str) and value:
            return value
    return None


class _ScopeMoved(Exception):
    """Internal: the APPLY body names a UE scope its bound policy does not carry."""


class R1Adapter:
    """Carries gateway commands over R1, and nothing else.

    Parameters
    ----------
    policy_port:
        Object shaped like :class:`R1PolicyPort`.
    policy_builder:
        ``builder(command) -> policy object``.  Gate 3 binds this to
        ``policy_translator.translate_intent`` with the deployment's discovery
        and capability manifest, so the body is schema-validated before it is
        ever sent.  Mandatory: a gateway that could send an unvalidated body
        would be the direct-control path section 9 forbids, wearing an R1 hat.
    near_rt_ric_id / policy_type_id:
        Fixed deployment identifiers; never taken from a command.
    readback_port:
        :class:`R1ReadbackPort`.  The contracted effect readback.  Absent,
        every read answers ``UNKNOWN`` and no trial can even be prepared --
        which is the correct behaviour for a deployment that cannot prove its
        own effects, and is why binding a real one is on Gate 3's critical
        path rather than beside it.
    status_projection:
        How a policy status becomes a configuration mapping; defaults to
        :func:`project_verified_readback`.
    binding_journal:
        Optional :class:`~assurance.gateway.r1_binding_journal.R1BindingJournal`.
        Absent, the transaction-to-policy binding lives in memory for the life
        of the process, which is what the steering path has always done.
        Present, the binding is written *before* the create/update it describes
        and the returned policy id is persisted *before* this adapter
        acknowledges, so a restart can query and continue rather than orphan a
        policy it can no longer name.
    policy_builder (revision contract):
        When the builder accepts a ``last_revision`` keyword -- the Campaign 5
        builder does -- this adapter passes the **durable** last A1 revision on
        every draft, and the builder allocates ``last_revision + 1`` for a body
        change at a higher fence.  The binding journal is therefore the single
        restart-safe revision authority: a rebuilt builder cannot restart the
        numbering at one behind a producer that has already seen three.  A
        builder without the keyword is called as before, which is the
        compatibility path for the steering translator.

        The revision the builder chose is read back out of the body it will
        send (``trace.revision``) and persisted verbatim; a body without that
        key is **refused** (``REJECTED``, nothing sent) rather than journalled
        against a guessed number.  ``trace.fencingToken`` is the Kernel's own
        and is never derived from, or confused with, the revision.
    scope_key:
        ``scope_key(policy_body) -> str``.  Required with a journal.  It is
        given the **body**, not the command scope, because the scope the
        producer owns is the one in the policy it will store: a supplementary
        control acts on a different UE than the plan's scope names, and keying
        the durable record off the plan scope would reserve the objective UE's
        scope while capping somebody else's.
    refusal_errors:
        Exception types that mean *the request was refused with no effect* --
        a schema validation answer, a scope-ownership conflict, an unknown
        policy type.  Those are decisions, not lost messages, so they report
        ``REJECTED`` on a mutating operation instead of ``UNKNOWN``.  Empty by
        default, which keeps the standing rule: a write whose fate is unknown
        is ``UNKNOWN``, because reporting a clean failure would tell the Kernel
        nothing happened when something may have.  Only a composition root
        knows which of its port's exceptions are refusals, so only it may
        narrow this.
    operation_journal:
        Optional
        :class:`~assurance.gateway.r1_operation_journal.R1OperationJournal`.
        Every policy-port call this adapter issues is appended to it with the
        outcome the port gave, so *how many writes happened* is an adapter
        record rather than an inference from somebody's bookkeeping.  One is
        created in memory when none is passed, because the count is what a
        refusal claim is made of and an adapter that cannot state it should not
        be easier to build than one that can.
    retain_binding_until_restore:
        When true a DELETE does **not** release the binding or the scope.  The
        A1 withdrawal only *starts* the restoration; the prior configuration is
        back when an independent readback says so, and releasing on the DELETE
        response would free the scope while the change may still be live
        (contract section 6). Requires a binding journal: without one neither
        the withdrawal acknowledgment nor its outstanding obligation survives.
    """

    actuator_path = ActuatorPath.OFFICIAL_ORAN_DYNAMIC

    #: The A1 policy path cannot host a contract watchdog.  The guard an
    #: ``AIC_UECellSteering_1.0.0`` policy carries -- ``rollbackPolicy``,
    #: ``constraints.actionDeadlineMs``, ``validity.expiresAt`` -- lives *in
    #: the policy body*, and the body does not exist until the policy is
    #: created at apply time.  Arming before the commit line would therefore be
    #: a promise rather than an observation, and the Kernel's rule exists to
    #: refuse exactly that.  So this adapter declares that it does not host,
    #: and the Kernel arms the contract watchdog on its own two mechanisms
    #: instead (SEAMS-GATE2.md section 8.3, judgement 2).
    hosts_watchdogs = False

    #: Set on a steering adapter.  An A1 DELETE of a PIN_TO_CELL policy ends
    #: its authority but does not hand the UE back, so an UNDO/HALT whose
    #: command names a baseline cell other than the one applied is written as
    #: an UPDATE pinning that baseline, read back, and only then withdrawn.
    restore_by_handover = False

    def __init__(
        self,
        *,
        policy_port: R1PolicyPort,
        policy_builder: Callable[[Mapping[str, Any]], Dict[str, Any]],
        near_rt_ric_id: str,
        policy_type_id: str,
        readback_port: Optional["R1ReadbackPort"] = None,
        status_projection: Callable[
            [Mapping[str, Any]], Optional[Mapping[str, Any]]
        ] = project_verified_readback,
        name: str = "r1",
        binding_journal: Optional[Any] = None,
        scope_key: Optional[Callable[[Mapping[str, Any]], str]] = None,
        refusal_errors: Tuple[type, ...] = (),
        operation_journal: Optional[Any] = None,
        retain_binding_until_restore: bool = False,
        clock: Optional[Callable[[], str]] = None,
    ) -> None:
        if policy_builder is None:
            raise ValueError("an R1 adapter without a validated policy builder cannot act")
        if retain_binding_until_restore and binding_journal is None:
            raise ValueError("retained restoration requires a binding journal")
        if binding_journal is not None and scope_key is None:
            raise ValueError(
                "a durable binding journal is keyed by the semantic scope; "
                "an adapter that cannot derive one would journal collisions"
            )
        self._port = policy_port
        self._build = policy_builder
        self._near_rt_ric_id = near_rt_ric_id
        self._policy_type_id = policy_type_id
        self._readback = readback_port
        self._project = status_projection
        self.name = name
        self._journal = binding_journal
        self._operations = (operation_journal if operation_journal is not None
                            else InMemoryR1OperationJournal())
        self._scope_key = scope_key
        self._refusal_errors = tuple(refusal_errors)
        self._builder_takes_last_revision = _accepts_last_revision(policy_builder)
        self._builder_takes_last_fence = _accepts_last_fencing_token(policy_builder)
        self._retain_binding = bool(retain_binding_until_restore)
        # 원장을 durable 파일로 남기면서 시계를 주지 않으면 모든 `at` 이 빈 문자열이
        # 되고, **그 원장으로는 시간 대조를 못 한다** -- 되읽기 실패가 언제 났는지
        # 물을 수 없다.  2026-09-17 에 joint_runtime 의 조종 경로가 정확히 그랬고
        # (다른 두 호출부는 `clock=clock.now` 를 주고 있어 비교로도 안 드러났다),
        # 조종 항목만 `"at": ""` 로 쌓였다.  조용히 쓸모없어지느니 즉시 거절한다.
        if clock is None and operation_journal is not None:
            raise ValueError(
                "%s: an operation journal needs a clock -- without one every "
                "`at` is empty and the journal cannot be correlated in time"
                % name)
        self._clock = clock if clock is not None else (lambda: "")
        #: Transaction id to policy id.  Derived only from the Kernel token.
        self._bindings: Dict[str, str] = {}
        #: policy id -> the producer's fencingToken as this adapter last knew it.
        #: The live read that fills it costs a round trip on the write path, and
        #: on 2026-09-17 paying it on *every* write moved the median supplementary
        #: write from 7.3 s to 9.9 s, stretched the observation window by ~10 s a
        #: trial and cost two sittings their remaining trials to ``LEASE_EXPIRED``.
        #: The producer accepted what we sent, so after the first read our own
        #: sent value is the authority; a refused write clears the entry and the
        #: next attempt reads again.
        self._fence_cache: Dict[str, int] = {}
        #: The last configuration this adapter observed per transaction, used as
        #: the pre-policy baseline the durable binding records.
        self._observed: Dict[str, Mapping[str, Any]] = {}
        self._calls = 0
        if binding_journal is not None:
            self.recover_bindings()

    # -- inspection --------------------------------------------------------

    def bound_policy(self, transaction_id: str) -> Optional[str]:
        """The policy id bound to *transaction_id*, if one was created."""
        return self._bindings.get(transaction_id)

    def bindings(self) -> Mapping[str, str]:
        """Every live transaction-to-policy binding."""
        return dict(self._bindings)

    def operations(self, transaction_id: Optional[str] = None
                   ) -> Tuple[R1Operation, ...]:
        """Every policy-port call this adapter issued, in order."""
        return self._operations.operations(transaction_id)

    def write_counts(self, transaction_id: Optional[str] = None) -> Dict[str, int]:
        """``applies`` / ``withdrawals`` / ``refused`` / ``unknown`` for a scope.

        Read from the journal's own tallies, which are kept as records are
        appended.  Deriving them by scanning :meth:`operations` would be wrong
        rather than merely slow: the entry ring keeps the most *recent* records,
        so a CREATE followed by more status reads than the ring holds would
        report zero applies while the durable file still held the write.

        ``refused`` counts calls the producer answered *no* to -- those reached
        nothing -- while ``unknown`` counts calls whose fate this adapter cannot
        state, in-flight records included, and which a recovery must therefore
        treat as possible writes.
        """
        counts = getattr(self._operations, "counts", None)
        if counts is None:  # a journal port that keeps no tallies of its own
            return write_counts(self._operations.operations(transaction_id))
        return counts(transaction_id)

    def unresolved_operations(self) -> Tuple[R1Operation, ...]:
        """Calls that left this process and never came back.

        Non-empty only after a crash between a policy-port call and its answer.
        Each one is a write that may have landed, and a restart must treat it
        as one.
        """
        unresolved = getattr(self._operations, "unresolved", None)
        return unresolved() if unresolved is not None else ()

    def durable_binding(self, transaction_id: str) -> Optional[R1BindingRecord]:
        """The journalled binding, or ``None`` with no journal or no record."""
        if self._journal is None:
            return None
        return self._journal.binding_for(transaction_id)

    def recover_bindings(self) -> Tuple[str, ...]:
        """Rebuild the in-memory bindings from the journal after a restart.

        Returns the transactions that still own a scope.  A binding found in
        :attr:`~assurance.gateway.r1_binding_journal.BindingState.RESTORE_PENDING`
        is *not* resolved: the policy was asked to go away and nobody has yet
        seen the baseline come back.
        """
        if self._journal is None:
            return ()
        outstanding = []
        for transaction_id in self._journal.transaction_ids():
            record = self._journal.binding_for(transaction_id)
            if record is None or not record.holds_scope:
                continue
            if record.policy_id is not None:
                self._bindings[transaction_id] = record.policy_id
                self._declare_policy_type(record.policy_id)
            outstanding.append(transaction_id)
        return tuple(outstanding)

    # -- the frozen adapter surface ---------------------------------------

    def dispatch(
        self, *, token: KernelToken, command: Mapping[str, Any]
    ) -> GatewayResult:
        """Perform one already-validated command; report what was observed."""
        operation = GatewayOperation(command["operation"])
        self._calls += 1
        reference = f"{self.name}:{operation.value.lower()}:{self._calls}"
        mutating = operation not in (GatewayOperation.VALIDATE, GatewayOperation.READ)
        try:
            return self._perform(operation, token, command, reference)
        except Exception as exc:  # transport, contract or validation failure
            refused = self._is_refusal(exc)
            if mutating and refused:
                # Downstream said no before accepting anything.  Nothing
                # reached the equipment, so this is a rejection and the Kernel
                # may abort rather than open a recovery.
                outcome = GatewayOutcome.REJECTED
                if operation is GatewayOperation.APPLY:
                    self._mark_refused(token.transaction_id)
            elif mutating:
                outcome = GatewayOutcome.UNKNOWN
            else:
                outcome = GatewayOutcome.ERROR
            return GatewayResult(
                outcome=outcome,
                evidence_refs=(reference,),
                # Carry the message, not just the class.  This detail is the only
                # record the Kernel keeps of WHY an apply failed, and dropping the
                # string left a live 2026-09-17 episode with two trials reading
                # "UNKNOWN APPLY failed: R1Error" and no way to tell a refused
                # policy type from a transport timeout without re-running the case.
                # The journal's own handler a few lines down already spells it this
                # way; the bound keeps a verbose transport error out of the trace.
                # 200 was too tight in turn.  The gateway wraps this in its own
                # "n/9 axes are live; <axis>: " and truncates again (gateway.py
                # _settle), so a producer 409 arrived cut at "{'error': 'policy u"
                # and every PARTIAL_APPLY trial of 2026-09-16 was undiagnosable.
                # Both caps had to move; widening only one changes nothing.
                detail=f"{operation.value} failed: {type(exc).__name__}: {exc}"[:400],
            )

    # -- internals ---------------------------------------------------------

    def _issue(
        self,
        operation: R1PolicyOperation,
        call: Callable[[], Any],
        *,
        token: KernelToken,
        reference: str,
        policy_id: Optional[str] = None,
        detail: str = "",
    ) -> Any:
        """Perform one port call, durable before it and resolved after it.

        Two records, in this order and never the other one:

        1. an ``IN_FLIGHT`` record, appended and ``fsync``-ed **before** the
           port is touched.  A crash after the call lands and before its answer
           arrives then leaves a durable record saying a write may have
           happened -- which is the only safe reading, and the one an
           "outcome-first" journal would lose exactly when it mattered;
        2. the terminal record, naming the in-flight one it settles.

        The counting treats an unresolved in-flight record as one possible
        write and its resolution as a replacement, so a completed call is
        counted once and a crashed one is never counted as zero.

        A refusal is classified here exactly as :meth:`dispatch` classifies it,
        so the journal and the returned :class:`GatewayResult` can never
        disagree about whether anything was sent.
        """
        if (self._policy_type_id == POWER_POLICY_TYPE and operation in (
                R1PolicyOperation.CREATE, R1PolicyOperation.UPDATE, R1PolicyOperation.DELETE)):
            # v5: the gNB refuses a cell-power write within 15 s of the last one.  Wait
            # here, before the durable IN_FLIGHT record and outside the R1 request timeout.
            wait = power_write_wait_s()
            if wait > 0:
                self._gate_sleep(wait)
        in_flight = self._append_operation(
            operation, R1OperationOutcome.IN_FLIGHT, token=token,
            reference=reference, policy_id=policy_id,
            detail=f"about to call the policy port: {detail}"[:200],
        )
        try:
            result = call()
        except Exception as exc:
            refused = self._is_refusal(exc)
            # A write that did not land leaves our idea of the producer's fence
            # unproven, so the next build must read it again rather than trust
            # what we sent.  This is the only invalidation the cache needs: the
            # sole way it goes stale is a write we believed but the producer
            # did not take.
            if policy_id is not None:
                self._fence_cache.pop(str(policy_id), None)
            self._append_operation(
                operation,
                (R1OperationOutcome.REFUSED if refused
                 else R1OperationOutcome.UNKNOWN),
                token=token, reference=reference, policy_id=policy_id,
                detail=f"{type(exc).__name__}: {exc}"[:200],
                resolves=in_flight,
            )
            raise
        self._append_operation(
            operation, R1OperationOutcome.ISSUED, token=token,
            reference=reference,
            policy_id=(policy_id if policy_id is not None
                       else _policy_id_of(result)),
            detail=detail,
            resolves=in_flight,
        )
        return result

    #: 2026-09-23: the Campaign-5 producer refuses a rollback DELETE when the UE has
    #: no unique fresh KPM header at that instant ("DELETE rollback gate failed: UE
    #: scope has no unique fresh KPM/header", HTTP 409, nothing written).  Measured
    #: over 2026-09-22/23: 9 such refusals on 8 boards, **none retried**, each ending
    #: the board in a lockdown ("withdrawal outcome is unconfirmed").  The gate is a
    #: momentary condition of the KPM stream, so the same DELETE is asked again a
    #: few times.  Every attempt is journalled; any other refusal is raised at once.
    GATE_RETRY_ATTEMPTS = 5
    GATE_RETRY_WAIT_S = 2.0
    #: Both phrases are the producer's identity gate (``oran/campaign5/live_worker.py``
    #: ``IdentityRefusal``): a UE-scoped write with no unique fresh KPM header, and a
    #: cell-scoped one with no fresh UE header on the bound cell.  Same momentary
    #: KPM gap, same "nothing written" answer, so the same bounded retry.
    _GATE_REFUSALS = ("no unique fresh KPM", "no fresh UE control header")

    #: HTTP statuses that mean the producer *decided*.  Twin of
    #: ``oran.rapp.r1_client.R1_DEFINITIVE_STATUSES``, which ``assurance/**`` may not
    #: import (the seam tests).  2026-09-23 audit: the steering routes
    #: (pin_to_cell, objective_runtime, joint) were built with no refusal types at
    #: all, so a producer 400/404/409 -- ``R1Refusal``, which carries ``status`` --
    #: arrived as UNKNOWN.  Classifying by the status the exception carries fixes
    #: every route at once.  A transport failure or a retryable status carries none
    #: of these and stays UNKNOWN.
    _DEFINITIVE_STATUSES = frozenset({400, 404, 409})

    #: Takeover assumes one writer at a time (Codex review 2026-09-26): boards run one by one
    #: under the bed lock, so a policy owning the power scope when a board starts belongs to an
    #: earlier, finished board.  With concurrent writers this must first prove ownership.
    # Campaign-5 power: "target scope already owned by policy <id>"; A1-P steering (relayed since
    # 2026-09-29): "UE scope is already owned by policy <id>."
    _OWNED_BY = re.compile(r"scope (?:is )?already owned by policy ([0-9a-fA-F-]{36})")

    def _retained_power_owner(self, exc: BaseException) -> Optional[str]:
        """The policy id a CREATE was refused for (409 '... scope already owned by policy
        <id>'), or None.  Cell power: cell settings outlive the board that set them.
        Steering too since 2026-09-29 board 917: a trial that attained was retained (its
        ue3 steering policy stays), a UE re-registered, the rebind case's adapter could not
        see the retained policy and every later ue3 steer was refused 409 -> EXECUTION_FAILURE.
        One writer at a time (boards run one by one), so the owner is a finished case's."""
        if getattr(exc, "status", None) != 409:
            return None
        found = self._OWNED_BY.search(str(exc))
        return found.group(1) if found else None

    def _is_refusal(self, exc: BaseException) -> bool:
        if self._refusal_errors and isinstance(exc, self._refusal_errors):
            return True
        return getattr(exc, "status", None) in self._DEFINITIVE_STATUSES

    @staticmethod
    def _gate_sleep(seconds: float) -> None:
        import time
        time.sleep(seconds)

    def _delete_with_gate_retry(self, bound: str, *, token: KernelToken, reference: str,
                                operation: GatewayOperation) -> None:
        self._issue_with_gate_retry(
            R1PolicyOperation.DELETE, lambda: self._port.delete_policy(bound),
            token=token, reference=reference, policy_id=bound,
            detail=f"A1 DELETE for {operation.value}")

    def _issue_with_gate_retry(self, operation: R1PolicyOperation, call: Callable[[], Any], *,
                               token: KernelToken, reference: str, policy_id: str,
                               detail: str) -> Any:
        """:meth:`_issue`, asked again while the producer's identity gate refuses.

        Every restoring write goes through here -- the DELETE, the hand-back
        UPDATE and the baseline rewrite -- because the gate refuses all three
        alike (2026-09-23 audit: only the DELETE was retried)."""
        for attempt in range(1, self.GATE_RETRY_ATTEMPTS + 1):
            try:
                return self._issue(
                    operation, call, token=token, reference=reference, policy_id=policy_id,
                    detail=(detail
                            + (f" (attempt {attempt} after the producer's rollback gate)"
                               if attempt > 1 else "")))
            except Exception as exc:
                if (not self._is_refusal(exc)
                        or not any(phrase in str(exc) for phrase in self._GATE_REFUSALS)
                        or attempt >= self.GATE_RETRY_ATTEMPTS):
                    raise
                self._gate_sleep(self.GATE_RETRY_WAIT_S)
        raise AssertionError("unreachable")  # pragma: no cover

    def _append_operation(
        self,
        operation: R1PolicyOperation,
        outcome: R1OperationOutcome,
        *,
        token: KernelToken,
        reference: str,
        policy_id: Optional[str],
        detail: str,
        resolves: Optional[int] = None,
    ) -> int:
        """Append one record and return its sequence."""
        sequence = self._operations.next_sequence()
        self._operations.append(R1Operation(
            sequence=sequence,
            transaction_id=token.transaction_id,
            operation=operation,
            outcome=outcome,
            adapter=self.name,
            policy_type_id=self._policy_type_id,
            policy_id=policy_id,
            fencing_token=token.fencing_token,
            reference=reference,
            at=self._clock(),
            detail=detail,
            resolves=resolves,
        ))
        return sequence

    def _perform(
        self,
        operation: GatewayOperation,
        token: KernelToken,
        command: Mapping[str, Any],
        reference: str,
    ) -> GatewayResult:
        transaction_id = token.transaction_id
        if (operation in (GatewayOperation.VALIDATE, GatewayOperation.APPLY)
                and self._withdrawal_outstanding(transaction_id)):
            return GatewayResult(
                outcome=GatewayOutcome.REJECTED,
                evidence_refs=(reference,),
                detail="restoration is outstanding; the existing policy binding cannot be replaced",
            )
        if operation is GatewayOperation.VALIDATE:
            # Discovery and body construction only.  No policy is created, so
            # a prepare failure leaves nothing to roll back (task section 6.2).
            self._issue(
                R1PolicyOperation.VALIDATE,
                lambda: self._port.get_policy_type(self._policy_type_id),
                token=token, reference=reference,
                detail="policy type discovered; nothing created")
            body = self._build_body(command, token)
            refusal = self._revision_refusal(body)
            if refusal is not None:
                # Nothing was created, so this is a clean rejection.
                return GatewayResult(
                    outcome=GatewayOutcome.REJECTED,
                    evidence_refs=(reference,),
                    detail=refusal,
                )
            self._reserve(transaction_id, command, body)
            return GatewayResult(
                outcome=GatewayOutcome.ACKED,
                evidence_refs=(reference,),
                detail="policy body built and validated; nothing created",
            )
        if operation is GatewayOperation.READ:
            return self._read(command, transaction_id, reference)
        if operation is GatewayOperation.APPLY:
            body = self._build_body(command, token)
            refusal = self._revision_refusal(body)
            if refusal is not None:
                # Refused before the reservation and before the port: nothing
                # reached the equipment.
                return GatewayResult(
                    outcome=GatewayOutcome.REJECTED,
                    evidence_refs=(reference,),
                    detail=refusal,
                )
            conflict = self._scope_conflict(transaction_id, command, body)
            if conflict is not None:
                # Another transaction still owns this (type, scope).  Refusing
                # here is a clean rejection: nothing was sent.
                return GatewayResult(
                    outcome=GatewayOutcome.REJECTED,
                    evidence_refs=(reference,),
                    detail=conflict,
                )
            # Durable before the write leaves the adapter: a process that dies
            # between here and the response must be found as "a write may have
            # happened", never as absent.
            self._reserve(transaction_id, command, body, detail="APPLY_IN_FLIGHT")
            bound = self._bindings.get(transaction_id)
            if bound is None:
                bound = self._adopt_takeable_policy(transaction_id, body)
            if bound is None:
                bound = self._adopt_own_finalized(transaction_id, command.get("value"))
            def _create_policy() -> str:
                def create() -> Any:
                    return self._issue(
                        R1PolicyOperation.CREATE,
                        lambda: self._port.create_policy(
                            self._near_rt_ric_id, self._policy_type_id, dict(body)),
                        token=token, reference=reference,
                        detail=f"policy created for {command['axis']}")
                try:
                    created = create()
                except Exception as exc:
                    owner = self._retained_power_owner(exc)
                    if owner is None:
                        raise
                    # v5 takeover (2026-09-26 board 675): the previous board finalized a
                    # cell-power policy that owns the scope until it expires, and this
                    # board's journal cannot see it to adopt it, so every CREATE was
                    # refused 409.  Withdraw it on the official path (the producer
                    # restores its snapshot baseline), then create ours.
                    self._issue(
                        R1PolicyOperation.DELETE, lambda: self._port.delete_policy(owner),
                        token=token, reference=reference, policy_id=owner,
                        detail=f"withdrew retained power policy {owner} owning the scope")
                    created = create()
                return str(created["policyId"])

            if bound is None:
                policy_id = _create_policy()
                self._bindings[transaction_id] = policy_id
                detail = f"policy created for {command['axis']}"
            else:
                # Rebuilding the body here cannot raise its revision: the
                # builder is drafted once per fence and returns those same bytes
                # at COMMIT ("same fencingToken cannot change the policy body"),
                # which is the invariant that makes PREPARE and COMMIT identical.
                # The takeover's revision therefore has to be right at PREPARE --
                # see ``_last_revision``, which seeds above every revision this
                # journal has recorded rather than level with it.
                #
                # Name the two numbers the producer compares.  A takeover PUT is
                # refused with "policy update requires a newer revision and
                # fencingToken" when EITHER is not strictly greater than the
                # adopted policy's, and the message does not say which -- which
                # cost a live 2026-09-17 trial and an hour of guessing.  The
                # values are the body's own, not a fabrication.
                sent = (body.get("trace") or {})
                seed = getattr(self, "_last_seed", None)
                # ``fenceSeed`` says whether the live read answered.  Without it
                # a seed that silently read ``None`` looks identical in the
                # journal to one that was never needed, because the Kernel's
                # fence wins whenever it is ahead -- which is every write except
                # the cross-case takeover this exists for.  That ambiguity is
                # what made the envelope bug invisible until it was read live.
                fence_seed = getattr(self, "_last_fence_seed", None)
                # 쓰기 전에 기억한다: 거절되면 아래에서 지운다.  성공 뒤에 기억하면
                # 예외가 그 줄을 건너뛰어 캐시가 영원히 낡은 채로 남는다.
                self._remember_fence(bound, body)
                try:
                    if self._scope_moved(bound, body):
                        raise _ScopeMoved()
                    self._issue(
                        R1PolicyOperation.UPDATE,
                        lambda: self._port.update_policy(bound, dict(body)),
                        token=token, reference=reference, policy_id=bound,
                        detail=(f"policy updated for {command['axis']} "
                                f"(revision={sent.get('revision')}, "
                                f"fencingToken={sent.get('fencingToken')}, "
                                f"seed={seed}, fenceSeed={fence_seed}, "
                                f"adopted={self._revision_of_policy(bound)})"))
                except _ScopeMoved:
                    # The UE re-registered: the producers refuse an UPDATE that
                    # moves a policy's UE scope (see ``_replace_policy``).
                    self._fence_cache.pop(str(bound), None)
                    policy_id = self._replace_policy(
                        transaction_id, bound, body, token=token, reference=reference,
                        detail=f"policy recreated for {command['axis']} on the UE's new id")
                    detail = (f"policy recreated for {command['axis']}: {bound} named "
                              f"the UE's id before it re-registered")
                except Exception as exc:
                    # 404 means the producer ANSWERED and does not have it: the
                    # policy expired while this adapter still held its id.
                    # Live validity is 0.9-5.4 minutes and a sitting runs eight,
                    # so a retention trial routinely addresses a policy the
                    # producer has already dropped (2026-09-17, twice: the second
                    # cost a retention its whole trial).  Updating a policy that
                    # is gone can never succeed; the scope it held is free, so
                    # the honest repair is to create the policy again.  Only 404
                    # -- a 409 means it IS there and someone else moved the
                    # fence, and a transport failure proves nothing at all.
                    if getattr(exc, "status", None) != 404:
                        raise
                    self._fence_cache.pop(str(bound), None)
                    policy_id = _create_policy()
                    self._bindings[transaction_id] = policy_id
                    detail = (f"policy recreated for {command['axis']}: the "
                              f"producer no longer has {bound}")
                else:
                    policy_id = bound
                    detail = f"policy updated for {command['axis']}"
            # The returned policy id is persisted before this adapter
            # acknowledges (contract section 3, COMMIT/APPLY).
            self._bind(transaction_id, policy_id, BindingState.BOUND, detail=detail)
            self.__dict__.setdefault("_applied_values", {})[transaction_id] = _same(command.get("value"))
            self._remember_scope(policy_id, body)
            # No observed_config_hash: an A1 create is evidence, not effect.
            return GatewayResult(
                outcome=GatewayOutcome.ACKED,
                evidence_refs=(reference, f"{self.name}:policy:{policy_id}"),
                detail=detail,
            )
        if operation in (GatewayOperation.UNDO, GatewayOperation.HALT):
            bound = self._bindings.get(transaction_id)
            # A DELETE the producer *refused* withdrew nothing, so it is asked
            # again below instead of being treated as outstanding.
            if (bound is not None and self._withdrawal_outstanding(transaction_id)
                    and not self._withdrawal_refused(transaction_id)):
                if self._withdrawal_unconfirmed(transaction_id):
                    # Q-3 (2026-09-24): the DELETE's answer was lost.  Read before
                    # acting -- this used to answer UNKNOWN forever even when the
                    # DELETE had landed.  Gone: the withdrawal happened.  Still
                    # there: it did not land, so it is sent again below.  A port
                    # that cannot read keeps the old answer; no blind retry.
                    gone = self._policy_gone(bound)
                    if gone is None:
                        return GatewayResult(
                            outcome=GatewayOutcome.UNKNOWN,
                            evidence_refs=(reference, f"{self.name}:policy:{bound}"),
                            detail="withdrawal outcome is unconfirmed; binding and scope remain held",
                        )
                    if gone:
                        return self._acknowledge_withdrawal(
                            transaction_id, bound, operation, reference,
                            detail=("A1 DELETE landed (its answer was lost; the producer "
                                    "no longer has the policy); awaiting independent "
                                    "baseline readback"))
                else:
                    # A repeated idempotent undo.  Stop and reverse rollback are two
                    # routes to one restoration obligation, and a watchdog fires both;
                    # sending a second DELETE would ask the xApp to restore a
                    # baseline it is already restoring.  The obligation stands until
                    # an independent readback says the baseline is back.
                    return GatewayResult(
                        outcome=GatewayOutcome.ACKED,
                        evidence_refs=(reference, f"{self.name}:policy:{bound}"),
                        detail="withdrawal already outstanding; awaiting the baseline readback",
                    )
            if bound is None and self._apply_in_flight(transaction_id):
                # Q-3: the create landed and its answer (the id) was lost.  Find it
                # at the producer so it is withdrawn instead of orphaned.
                bound = self._adopt_lost_create(transaction_id)
            if bound is None:
                # Before concluding there is nothing to withdraw, look for a
                # policy a FINISHED transaction left live on this scope.  A
                # supplementary axis returns to its sentinel only by withdrawing
                # a policy, and after a settlement the Kernel grants no STOP to
                # the trial that created it, so retention -- which restores the
                # baseline -- had no way to express the restore and reported
                # "qualified=False ... first needs that live policy withdrawn"
                # (v4's first full episode, 2026-09-17, on dlPrbCap@ue2).
                # Adopting it makes the withdrawal expressible by the
                # transaction that is actually running, and the ordinary
                # DELETE-then-readback obligation below applies unchanged: the
                # scope stays held until the baseline is independently observed.
                bound = self._adopt_takeable_policy(transaction_id)
            if bound is None:
                # No policy was ever created -- an APPLY that never ran, or one
                # the producer refused before creating anything.  There is
                # nothing to withdraw, but a reservation this transaction placed
                # at VALIDATE still holds the (type, scope): releasing it here
                # is what lets a *later* transaction reserve the same scope.
                # Without this a joint case's second cap trial is refused
                # ``one active policy per scope`` by the first trial's dead
                # reservation (observed over the air 2026-09-06).
                if self._apply_in_flight(transaction_id):
                    # 2026-09-23 audit: the POST left and its answer did not come
                    # back, so a policy may exist whose id this adapter never
                    # learned.  "Nothing to withdraw" would be a claim nobody
                    # observed; the scope stays held and the outcome is unknown.
                    return GatewayResult(
                        outcome=GatewayOutcome.UNKNOWN,
                        evidence_refs=(reference,),
                        detail=("an APPLY may have created a policy whose id never "
                                "arrived; nothing can be withdrawn by id and the "
                                "scope stays held"),
                    )
                released = self._release_reservation(transaction_id)
                return GatewayResult(
                    outcome=GatewayOutcome.ACKED,
                    evidence_refs=(reference,),
                    detail=("no policy bound to this transaction; released the "
                            "standing reservation" if released
                            else "no policy bound to this transaction; nothing to withdraw"),
                )
            # A withdrawal cannot restore a baseline that is not the sentinel.
            # ``A1 DELETE`` asks the Campaign-5 worker to roll back to the
            # snapshot it took when the policy was *created*
            # (``live_worker.py``: ``if entry is None: ... "baseline": baseline``
            # -- later writes to the same policy refresh only ``notAfter``), and
            # scope takeover makes one policy span several trials.  So a trial
            # that returned an axis to its sentinel has a baseline of, say, 18
            # while DELETE lands on 0, and the reversal is judged
            # ``PARTIAL_APPLY -- reversal did not restore the baseline
            # configuration``: two live episodes of 2026-09-16/17 ended
            # ``RECOVERY_FAILURE`` with retention blocked for exactly this.
            #
            # Where the two disagree, restore by *writing* the baseline back.
            # The policy stays live because a non-sentinel value can only be
            # expressed by a live policy, and the binding is left in the shape a
            # settled trial leaves -- ``BOUND`` and ``finalized``, therefore
            # ``takeable`` -- so the next transaction on this scope adopts it
            # instead of being refused ``one active policy per scope``.
            restored = self._restore_by_handover(operation, token, command, reference, bound)
            if restored is not None:
                return restored
            # The hand-back may have replaced the policy (a re-registered UE).
            bound = self._bindings.get(transaction_id, bound)
            rewrite = self._baseline_needing_rewrite(transaction_id, bound)
            if rewrite is not None:
                axis, value = rewrite
                body = self._build_body({**dict(command), "axis": axis,
                                         "value": value}, token)
                self._journal_revision(transaction_id, body)
                if self._scope_moved(bound, body):
                    bound = self._replace_policy(
                        transaction_id, bound, body, token=token, reference=reference,
                        detail=(f"baseline {value!r} for {axis} written on the UE's "
                                f"new id for {operation.value}"))
                else:
                    self._issue_with_gate_retry(
                        R1PolicyOperation.UPDATE,
                        lambda: self._port.update_policy(bound, dict(body)),
                        token=token, reference=reference, policy_id=bound,
                        detail=(f"baseline rewritten for {axis} to {value!r} for "
                                f"{operation.value}; a withdrawal would have left "
                                f"the sentinel"))
                    # 2026-09-29 board 918 (rebind case): the HALT rewrite went out at the cached
                    # adopted fence + 1 and the cache was not advanced, so the UNDO rewrite drafted
                    # the same fence and Campaign-5 refused it "requires a newer revision and
                    # fencingToken" -> INCIDENT_LOCKDOWN.  What the producer took is what it holds.
                    self._remember_fence(bound, body)
                self._bind(
                    transaction_id, bound, BindingState.BOUND,
                    detail=(f"baseline {value!r} rewritten for {axis}; the policy "
                            f"stays live and is takeable"),
                    finalized=True,
                )
                return GatewayResult(
                    outcome=GatewayOutcome.ACKED,
                    evidence_refs=(reference, f"{self.name}:policy:{bound}"),
                    detail=(f"baseline rewritten for {axis} to {value!r}; "
                            f"the policy stays live"),
                )
            # Journalled before the withdrawal, so a crash between the two
            # leaves a binding that says a restore is owed.
            before = self.durable_binding(transaction_id)
            self._bind(
                transaction_id, bound, BindingState.RESTORE_PENDING,
                detail="A1 DELETE sent; restoration not yet observed",
            )
            try:
                self._delete_with_gate_retry(bound, token=token, reference=reference,
                                             operation=operation)
            except Exception as exc:
                # 2026-09-23 audit: a refusal (a 409 gate that outlasted its
                # retries included) is the producer saying it withdrew nothing.
                # Left as a plain unacknowledged RESTORE_PENDING, every later
                # UNDO answered "withdrawal outcome is unconfirmed" and never
                # sent the DELETE again.  Say it was refused, so the next undo
                # asks again.  The binding stays RESTORE_PENDING rather than
                # going back to BOUND: the policy is still live, and a BOUND
                # read could confirm the baseline from another cell's equal
                # scalar (test_r1_withdrawal_obligations).  A lost answer is not
                # a refusal and keeps the plain "sent" record (UNKNOWN).
                if before is not None and self._is_refusal(exc):
                    self._bind(
                        transaction_id, bound, BindingState.RESTORE_PENDING,
                        detail=(f"{self._DELETE_REFUSED}: "
                                f"{type(exc).__name__}: {exc}")[:200])
                raise
            # Durable only after the port returned. In the live R1 client this
            # means HTTP 204, which the Campaign-5 worker emits after verifying
            # its original-owner rollback. An exception/crash never implies ACK.
            return self._acknowledge_withdrawal(
                transaction_id, bound, operation, reference,
                detail="A1 DELETE acknowledged; awaiting independent baseline readback")
        if operation is GatewayOperation.FINALIZE:
            bound = self._bindings.get(transaction_id)
            if bound is None:
                return GatewayResult(
                    outcome=GatewayOutcome.UNKNOWN,
                    evidence_refs=(reference,),
                    detail="no policy bound to this transaction; nothing to finalize",
                )
            status = self._issue(
                R1PolicyOperation.STATUS,
                lambda: self._port.get_policy_status(bound),
                token=token, reference=reference, policy_id=bound,
                detail="policy status read for finalize")
            projected = self._project(status)
            aic = status.get("aicStatus") if isinstance(status, Mapping) else None
            terminal = isinstance(aic, Mapping) and aic.get("episodeTerminal") is True
            # A policy that simply ran out its validity after a VERIFIED
            # readback, with no rollback asked for, ended its authority, not its
            # effect: a PIN_TO_CELL handover is not undone by expiry (the
            # producer's DELETE and EXPIRED paths never steer back).  2026-09-19
            # boards 093728 and 110539: the steering policy's validity is the
            # COMMIT lease (hold + two deadlines), the trailing collection ran
            # past it, and FINALIZE 11 s later read NOT_ENFORCED/EXPIRED --
            # UNKNOWN on a handover the reread had just confirmed, then a
            # lockdown.  The independent contracted readback below still has to
            # confirm the configuration, so this admits nothing unobserved.
            expired_after_verified = (
                isinstance(aic, Mapping) and aic.get("policyState") == "EXPIRED"
                and isinstance(aic.get("rollback"), Mapping)
                and aic["rollback"].get("state") == "NOT_REQUESTED"
                and projected is not None)
            if terminal and not expired_after_verified and (
                    status.get("enforceStatus") != "ENFORCED" or projected is None):
                return GatewayResult(
                    outcome=GatewayOutcome.UNKNOWN,
                    evidence_refs=(reference, f"{self.name}:policy:{bound}"),
                    detail="acceptance without a verified readback is not a finalized effect",
                )
            result = self._read(
                command,
                transaction_id,
                reference,
                extra_refs=(f"{self.name}:policy:{bound}",),
                detail=("finalize acknowledged with a verified readback" if projected is not None
                        else "status is non-terminal; polling the contracted readback"),
            )
            if result.outcome is GatewayOutcome.ACKED:
                # The trial settles here and its transaction will never write
                # again, but the policy stays live because that is what
                # retention means.  Recording the two facts separately is what
                # lets a later trial take the scope over instead of finding it
                # frozen for the rest of the episode.
                self._mark_finalized(transaction_id)
            return result
        return GatewayResult(
            outcome=GatewayOutcome.ERROR,
            evidence_refs=(reference,),
            detail=f"unsupported operation {operation.value}",
        )

    def _acknowledge_withdrawal(self, transaction_id: str, bound: str,
                                operation: GatewayOperation, reference: str, *,
                                detail: str) -> GatewayResult:
        """Record a withdrawal the producer confirmed; the baseline readback still owes."""
        self._bind(transaction_id, bound, BindingState.RESTORE_PENDING,
                   detail=detail, withdrawal_acknowledged=True)
        if not self._retain_binding:
            # Dropped only after the delete returns: a binding kept for a
            # policy that is gone would block the next scope, and a binding
            # dropped for one that is not would strand it (GAP-04).
            self._bindings.pop(transaction_id, None)
            self._release(transaction_id, detail="withdrawn")
        return GatewayResult(
            outcome=GatewayOutcome.ACKED,
            evidence_refs=(reference, f"{self.name}:policy:{bound}"),
            detail=(
                f"policy {'withdrawn' if operation is GatewayOperation.HALT else 'reversed'}"
                + ("; binding held until the baseline is read back"
                   if self._retain_binding else "")
            ),
        )

    def _policy_gone(self, policy_id: str) -> Optional[bool]:
        """Whether the producer no longer has *policy_id*: True on a 404, False when
        it answers, ``None`` when the port cannot read by id or the read fails."""
        getter = getattr(self._port, "get_policy", None)
        if getter is None:
            return None
        try:
            getter(policy_id)
        except Exception as exc:  # noqa: BLE001 - only a 404 is an answer
            return True if getattr(exc, "status", None) == 404 else None
        return False

    def _read(
        self,
        command: Mapping[str, Any],
        transaction_id: str,
        reference: str,
        extra_refs: tuple = (),
        detail: str = "contracted configuration readback",
    ) -> GatewayResult:
        if self._withdrawal_unconfirmed(transaction_id):
            return GatewayResult(
                outcome=GatewayOutcome.UNKNOWN,
                evidence_refs=(reference,) + extra_refs,
                detail="withdrawal outcome is unconfirmed; a configuration scalar cannot prove recovery",
            )
        if self._readback is None:
            return GatewayResult(
                outcome=GatewayOutcome.UNKNOWN,
                evidence_refs=(reference,) + extra_refs,
                detail="no contracted effect readback is bound; an acceptance is not one",
            )
        # The policy id travels with the request: the contracted readback for
        # an applied change is the one the policy status carries, and it
        # cannot be reached from the scope alone.  Before a policy exists it
        # is ``None``, which is a different question with a different source,
        # not a missing argument.
        policy_id = self._restoring_policy_id(transaction_id)
        observed = self._readback(
            scope=command["scope"],
            transaction_id=transaction_id,
            policy_id=policy_id,
        )
        if observed is None and policy_id is not None and self._radio_did_not_move(command):
            # The write was admitted, the gNB prepared the handover, the AMF
            # answered -- and the UE never arrived on the target cell (2026-09-20
            # boards 132028 and 140425: ue3 stayed on 12345678 for ten minutes
            # while the policy named 87654321).  The policy status can never say
            # VERIFIED for a move the radio did not make, so the corroborated
            # readback answers nothing and the trial locks the board down.  The
            # independent counter *did* answer the whole time: it shows the UE on
            # its baseline, which is exactly what recovery has to prove.  Read
            # that way, and let the trial fail as NOT_ATTAINED instead of taking
            # the board with it.
            observed = self._readback(scope=command["scope"],
                                      transaction_id=transaction_id, policy_id=None)
            if observed is not None:
                detail = detail + " (independent counter; the radio did not move)"
        refusal = (self._refused_without_writing(policy_id)
                   if observed is None and policy_id is not None else None)
        if refusal is not None:
            # The producer admitted nothing to the radio: its status carries an
            # error with ``writeMayHaveOccurred: false`` (2026-09-20, board
            # 20260919T193249 trial 6: AIC_SCOPE_NOT_FOUND at ADMISSION).  That
            # status never says VERIFIED, so reading "through" it answered
            # UNKNOWN until the trial locked down.  Read the pre-policy way --
            # the independent counter alone -- so the commit sees the baseline
            # and the ordinary rollback closes the trial.
            observed = self._readback(scope=command["scope"],
                                      transaction_id=transaction_id, policy_id=None)
            detail = f"{detail} (producer refused without writing -- {refusal})"
        if observed is None:
            return GatewayResult(
                outcome=GatewayOutcome.UNKNOWN,
                evidence_refs=(reference,) + extra_refs,
                detail=("the contracted readback did not produce an observation"
                        + (f"; producer refused without writing -- {refusal}"
                           if refusal is not None else "")),
            )
        self._observed[transaction_id] = dict(observed)
        detail = self._confirm_restore(transaction_id, observed, detail)
        return GatewayResult(
            outcome=GatewayOutcome.ACKED,
            observed_config_hash=config_hash(observed),
            observed_config=dict(observed),
            evidence_refs=(reference,) + extra_refs,
            detail=detail,
        )

    def _withdrawal_outstanding(self, transaction_id: str) -> bool:
        """True while a DELETE has gone and the baseline has not come back."""
        if self._journal is None:
            return False
        record = self._journal.binding_for(transaction_id)
        return record is not None and record.state is BindingState.RESTORE_PENDING

    _DELETE_REFUSED = "A1 DELETE refused, nothing withdrawn"

    def _withdrawal_refused(self, transaction_id: str) -> bool:
        """True when the last DELETE was refused by the producer (nothing written)."""
        record = self.durable_binding(transaction_id)
        return (record is not None and record.state is BindingState.RESTORE_PENDING
                and not record.withdrawal_acknowledged
                and record.detail.startswith(self._DELETE_REFUSED))

    def _withdrawal_unconfirmed(self, transaction_id: str) -> bool:
        record = self.durable_binding(transaction_id)
        return (record is not None and record.state is BindingState.RESTORE_PENDING
                and not record.withdrawal_acknowledged)


    def _radio_did_not_move(self, command: Mapping[str, Any]) -> bool:
        """True when this adapter writes a move the radio has to execute.

        Only the steering axis can be commanded and not obeyed: a scheduler
        weight or a PRB cap takes effect where it is written, so a missing
        readback there is a real unknown and must stay one.  ``restore_by_handover``
        is set on the steering adapters alone (joint_runtime, objective_runtime,
        pin_to_cell_driver), and a READ carries
        no axis of its own, so the flag is the whole question.
        """
        return bool(self.restore_by_handover)

    def _restoring_policy_id(self, transaction_id: str) -> Optional[str]:
        """The policy to read a configuration back through, or ``None``.

        Once a withdrawal is acknowledged there is no policy left to ask:
        the A1 DELETE returned, and a status read of a withdrawn policy reports the
        episode that applied the change rather than the restoration.  So a
        binding in
        :attr:`~assurance.gateway.r1_binding_journal.BindingState.RESTORE_PENDING`
        reads through the *pre-policy* path -- the independent configuration
        counter alone -- which is exactly what the contract calls recovery:
        "an independent readback equals the prior value".  The binding and the
        scope lock stay held until that read succeeds; only the source of the
        observation changes.
        """
        binding = self._bindings.get(transaction_id)
        if binding is None or self._journal is None:
            return binding
        record = self._journal.binding_for(transaction_id)
        if record is not None and record.state is BindingState.RESTORE_PENDING:
            return None
        return binding

    def _refused_without_writing(self, policy_id: str) -> Optional[str]:
        """Why the producer refused this policy without writing, or ``None``.

        Only an explicit ``writeMayHaveOccurred: false`` counts; a missing or
        true flag, or a status that cannot be read, keeps the policy path
        (fail-closed).

        Returns the producer's own words -- ``"<stage>: <code>"`` -- because the
        boolean this used to be threw them away.  2026-09-23: every steering
        trial of the preceding night died as ``PARTIAL_APPLY 9/9 acknowledged
        (the readback confirmed none)`` with an *empty* reason, while the
        producer's ``status_events`` row said ``AIC_E2_NOT_READY`` at
        ``ADMISSION`` 62 ms after the create.  The gateway held the answer and
        did not say it, so the hunt went to ``actionDeadlineMs`` instead -- the
        wrong layer entirely.  A refusal that is observed must be quoted.
        """
        try:
            status = self._port.get_policy_status(policy_id)
        except Exception:  # noqa: BLE001 - unreadable proves nothing
            return None
        aic = status.get("aicStatus") if isinstance(status, Mapping) else None
        error = aic.get("error") if isinstance(aic, Mapping) else None
        if not isinstance(error, Mapping) or error.get("writeMayHaveOccurred") is not False:
            return None
        stage = str(error.get("stage") or "?")
        code = str(error.get("code") or "?")
        return f"{stage}: {code}"

    # -- the durable binding ----------------------------------------------

    def _build_body(self, command: Mapping[str, Any], token: KernelToken) -> Dict[str, Any]:
        """Build the policy body, seeded from the durable revision.

        The seed is the journal's, not a closure's: a builder rebuilt after a
        restart has no memory, and one that restarted its numbering at revision
        one would be refused by a producer that had already stored three.

        With no journal there is nothing durable to seed *from*, and the
        keyword is not passed at all: sending zero would assert that no
        revision has ever been issued, and the builder would then refuse its
        own in-process history at the next higher fence.  A journal-less
        adapter keeps the builder's own numbering, which is exactly as
        restart-safe as its in-memory bindings are -- that is, not at all, and
        openly so.
        """
        if not self._builder_takes_last_revision or self._journal is None:
            return self._build(command)
        seed = self._last_revision(token.transaction_id)
        # Remember what the seed was, so a refused takeover PUT can be read
        # without re-deriving it.  On 2026-09-17 the adopting body kept sending
        # the revision the adopted policy already held, the seed lift did not
        # fire, and nothing in the record said which branch of ``_last_revision``
        # had answered.
        self._last_seed = seed
        fence = self._live_fencing_token()
        self._last_fence_seed = fence
        if fence is None:
            return self._build(command, last_revision=seed)
        return self._build(command, last_revision=seed, last_fencing_token=fence)

    def _live_fencing_token(self) -> Optional[int]:
        """The highest ``trace.fencingToken`` the producer holds for this type.

        Only for policies this journal marks ``takeable`` -- a policy a finished
        transaction left live, which is the only thing a later transaction can
        adopt.  The Kernel's fence is per-case: a retention case opens a fresh
        event store, its fences restart at zero, and the PUT that adopts the
        search case's policy is refused with "policy update requires a newer
        revision and fencingToken" because 2 does not exceed 33.  Three live
        episodes lost their retention to exactly that (2026-09-17).

        The producer is the authority on its own fence, so ask it.  A port
        without ``get_policy``, an unreadable policy, or a body without the key
        all answer ``None`` and the Kernel's fence is sent verbatim -- the
        behaviour before this existed.  A read is never allowed to fail a write.
        """
        if (self._journal is None or not self._builder_takes_last_fence
                or not hasattr(self._port, "get_policy")):
            return None
        best: Optional[int] = None
        for key in self._journal.transaction_ids():
            owner = self._journal.binding_for(key)
            if owner is None or owner.policy_type_id != self._policy_type_id:
                continue
            pid = str(owner.policy_id)
            if not owner.takeable:
                # Adopting a policy makes the binding the adopter's own, so it is
                # no longer takeable -- but the producer still holds the lifted
                # fence we sent.  Without this floor the adopter's own STOP went
                # out at the Kernel's fence (3 against 27) and was refused 409:
                # rollback PARTIAL_APPLY, incident lockdown (2026-09-25 board 604).
                # Only what we sent and the producer took; no new read.
                if pid in self._fence_cache:
                    value = self._fence_cache[pid]
                    best = value if best is None else max(best, value)
                continue
            if pid in self._fence_cache:
                value: Any = self._fence_cache[pid]
            else:
                try:
                    live = self._port.get_policy(pid)
                except Exception:  # noqa: BLE001 - a read must never fail a write
                    continue
                value = _trace_of(live).get("fencingToken")
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    continue
                self._fence_cache[pid] = value
            best = value if best is None else max(best, value)
        return best

    def _remember_fence(self, policy_id: Any, body: Mapping[str, Any]) -> None:
        """What we sent and the producer accepted is what it now holds."""
        value = (dict(body.get("trace") or {})).get("fencingToken")
        if not isinstance(value, bool) and isinstance(value, int) and value >= 0:
            self._fence_cache[str(policy_id)] = value

    def _last_revision(self, transaction_id: str) -> int:
        """The last A1 revision this scope durably owns, or zero.

        Per transaction when a binding exists -- that record holds the revision
        the adapter last *sent*.  Otherwise the highest this journal has seen
        for the policy type, which is strictly stronger than per-scope
        monotonicity and is the only thing that can be asked before the body,
        and therefore the scope key, exists.
        """
        if self._journal is None:  # pragma: no cover - _build_body precedes us
            return 0
        record = self._journal.binding_for(transaction_id)
        if record is not None:
            return record.policy_revision
        seed = max(0, self._journal.next_revision_for_type(self._policy_type_id) - 1)
        # A transaction with no record of its own may be about to take a scope
        # over, and the producer refuses an update that merely TIES the live
        # policy ("policy update requires a newer revision and fencingToken",
        # observed live 2026-09-17 with the adopted binding and the adopting one
        # both at revision 2).  The body is drafted once per fence and the
        # builder returns those same bytes at COMMIT, so the revision cannot be
        # raised at APPLY -- it has to be right here.  Seeding from the takeable
        # owners keeps the first revision at one on an empty journal, which is
        # the contract ``test_the_first_body_carries_revision_one_at_fence_zero``
        # pins, and only lifts the seed where a live policy could be adopted.
        for key in self._journal.transaction_ids():
            owner = self._journal.binding_for(key)
            if (owner is not None and owner.takeable
                    and owner.policy_type_id == self._policy_type_id):
                seed = max(seed, owner.policy_revision)
        return seed

    @staticmethod
    def drafted_revision(body: Mapping[str, Any]) -> Optional[int]:
        """The A1 revision the builder put in the body, or ``None``.

        ``trace.revision`` and nothing else.  ``trace.fencingToken`` is the
        Kernel's fence and starts at zero; reading it here would journal a
        revision the producer never saw and would break the one guarantee this
        record exists to give.
        """
        revision = (body.get("trace") or {}).get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            return None
        return revision if revision >= 1 else None

    def _scope_conflict(
        self, transaction_id: str, command: Mapping[str, Any], body: Mapping[str, Any]
    ) -> Optional[str]:
        del command
        if self._journal is None or self._scope_key is None:
            return None
        key = self._scope_key(body)
        # Not ``scope_owner``: by APPLY this transaction has its own VALIDATE
        # reservation in the journal and ``RESERVED`` holds the scope, so
        # ``scope_owner`` can hand back *us* -- whichever id sorts first -- and
        # this check then fails open, letting a genuine conflict through to the
        # producer's 409 instead of refusing cleanly with nothing sent.  Same
        # defect as ``_takeable_owner`` documents; ask about other transactions.
        owner = self._other_holder(transaction_id, key)
        if owner is not None and owner.takeable:
            # Its creator is finished; adopting the policy keeps one policy on
            # the scope (``_adopt_takeable_policy`` turns the APPLY into an
            # in-place update) and no second row is created.
            return None
        if owner is not None:
            return (
                f"{self._policy_type_id} scope is still owned by transaction "
                f"{owner.transaction_id} ({owner.state.value}); one active policy "
                "per (policy type, semantic scope)"
            )
        return None

    def _revision_refusal(self, body: Mapping[str, Any]) -> Optional[str]:
        """Why this body cannot be journalled, or ``None``.

        A body with no ``trace.revision`` cannot be recorded honestly: the
        journal would have to invent a number, and after a higher fence the
        body the producer stored and the record this process kept would hold
        different revisions -- the producer then refusing a valid update as
        non-monotonic.  So it is refused before anything is sent.
        """
        if self._journal is None:
            return None
        if self.drafted_revision(body) is None:
            return (f"{self._policy_type_id} body carries no trace.revision; a "
                    "durable binding cannot record a revision the producer was "
                    "never sent")
        return None

    def _reserve(
        self,
        transaction_id: str,
        command: Mapping[str, Any],
        body: Mapping[str, Any],
        detail: str = "scope reserved; nothing created",
    ) -> None:
        if self._journal is None or self._scope_key is None:
            return
        key = self._scope_key(body)
        current = self._journal.binding_for(transaction_id)
        # The builder already chose the revision and put it in the body it will
        # send; this records *that* number and no other.  There is deliberately
        # no fallback: a journal that allocated one of its own would record a
        # revision the producer never saw, and the callers refuse a body
        # without the key before reaching here.
        revision = self.drafted_revision(body)
        if revision is None:  # pragma: no cover - _revision_refusal precedes us
            raise ValueError("a policy body without trace.revision cannot be journalled")
        self._journal.write(
            R1BindingRecord(
                transaction_id=transaction_id,
                policy_type_id=self._policy_type_id,
                scope_key=key,
                state=current.state if current is not None else BindingState.RESERVED,
                policy_revision=revision,
                policy_id=current.policy_id if current is not None else None,
                baseline_config=(
                    current.baseline_config
                    if current is not None and current.baseline_config
                    else self._observed.get(transaction_id, {})
                ),
                updated_at=self._clock(),
                detail=detail,
                withdrawal_acknowledged=False,
            )
        )

    def _bind(
        self, transaction_id: str, policy_id: str, state: BindingState, *, detail: str,
        withdrawal_acknowledged: bool = False, finalized: Optional[bool] = None,
    ) -> None:
        if self._journal is None:
            return
        current = self._journal.binding_for(transaction_id)
        if current is None:
            return
        self._journal.write(
            current.evolve(
                state=state, policy_id=policy_id, updated_at=self._clock(), detail=detail,
                withdrawal_acknowledged=withdrawal_acknowledged,
                finalized=current.finalized if finalized is None else finalized,
            )
        )

    def _scope_identity(self, body: Mapping[str, Any]) -> Tuple[Any, Optional[str]]:
        return (_same(body.get("scope")),
                None if self._scope_key is None else self._scope_key(body))

    def _remember_scope(self, policy_id: str, body: Mapping[str, Any]) -> None:
        self.__dict__.setdefault("_policy_scopes", {})[str(policy_id)] = \
            self._scope_identity(body)

    def _scope_moved(self, policy_id: str, body: Mapping[str, Any]) -> bool:
        """Whether *body* names a different UE scope than *policy_id* carries.

        Known from this adapter's own writes, else from the journal's scope key
        for that policy (a policy adopted from an earlier sitting).  Unknown
        means "not moved": the UPDATE goes out exactly as before.
        """
        known = self.__dict__.setdefault("_policy_scopes", {}).get(str(policy_id))
        if known is not None:
            return known != self._scope_identity(body)
        if self._journal is None or self._scope_key is None:
            return False
        for key in self._journal.transaction_ids():
            record = self._journal.binding_for(key)
            if record is not None and record.policy_id == policy_id and record.scope_key:
                return record.scope_key != self._scope_key(body)
        return False

    def _replace_policy(self, transaction_id: str, bound: str, body: Mapping[str, Any], *,
                        token: KernelToken, reference: str, detail: str) -> str:
        """Withdraw *bound* and CREATE *body* as a fresh policy; the new id.

        2026-09-24 board 151803: a UE that re-registered comes back under a new
        id, the builder follows the role to it (board 123728), and both
        producers refuse an UPDATE that moves a policy's UE scope -- R1 409
        "scope.ueId cannot change on update", A1-P "A policy update cannot
        change its UE scope" (relayed as 503 -> UNKNOWN), Campaign-5 "policy id
        cannot move to a different target scope".  Two trials locked down and
        the board ended RECOVERY_FAILURE.  The old policy names a dead UE
        context, so it is withdrawn and the write goes out as a CREATE on the
        live id.  Every UPDATE site (APPLY takeover, hand-back, baseline
        rewrite) routes a moved scope through here.

        A 404 on the DELETE means the policy is already gone -- a previous call
        withdrew it and then failed to CREATE -- so the CREATE is simply asked
        again instead of the undo being refused forever.
        """
        try:
            self._issue_with_gate_retry(
                R1PolicyOperation.DELETE, lambda: self._port.delete_policy(bound),
                token=token, reference=reference, policy_id=bound,
                detail=f"A1 DELETE of {bound}: its UE re-registered under a new id")
        except Exception as exc:
            if getattr(exc, "status", None) != 404:
                raise
        # Q-3: a previous CREATE here may have landed with its answer lost.  Asking
        # again would leave that one orphaned, so adopt it when the producer holds
        # exactly this body on the new scope.
        found = (None if self._scope_key is None
                 else self._unnamed_policy_on(self._scope_key(body), body))
        if found is not None:
            policy_id = found
        else:
            created = self._issue(
                R1PolicyOperation.CREATE,
                lambda: self._port.create_policy(
                    self._near_rt_ric_id, self._policy_type_id, dict(body)),
                token=token, reference=reference,
                detail=f"{detail}; {bound} named the id it had before re-registering")
            policy_id = str(created["policyId"])
        self._bindings[transaction_id] = policy_id
        self._remember_scope(policy_id, body)
        self._bind(transaction_id, policy_id, BindingState.BOUND,
                   detail="policy recreated for the re-registered UE")
        if self._journal is not None and self._scope_key is not None:
            current = self._journal.binding_for(transaction_id)
            if current is not None:
                self._journal.write(current.evolve(scope_key=self._scope_key(body)))
        return policy_id

    def _unnamed_policy_on(self, scope_key: Optional[str],
                           body: Optional[Mapping[str, Any]] = None) -> Optional[str]:
        """After a lost CREATE: the policy the producer holds on *scope_key* that
        no binding here names -- the write that landed without its answer (Q-3).

        With *body*, only a policy storing exactly that body counts.  ``None``
        when nothing matches, or when the port cannot list and read policies by
        id -- then the id stays unknown, as it always was.
        """
        lister = getattr(self._port, "list_policies", None)
        getter = getattr(self._port, "get_policy", None)
        if lister is None or getter is None or self._scope_key is None or scope_key is None:
            return None
        named = {str(pid) for pid in self._bindings.values()}
        if self._journal is not None:
            for key in self._journal.transaction_ids():
                record = self._journal.binding_for(key)
                if record is not None and record.policy_id:
                    named.add(str(record.policy_id))
        try:
            listed = lister(near_rt_ric_id=self._near_rt_ric_id,
                            policy_type_id=self._policy_type_id)
            for item in listed or ():
                # ponytail: a listing that carries no ids (the nonrt R1 list returns
                # bare PolicyObjectInformation) cannot name the orphan; it stays unknown.
                pid = item if isinstance(item, str) else (
                    item.get("policyId") if isinstance(item, Mapping) else None)
                if not pid or str(pid) in named:
                    continue
                stored = getter(str(pid))
                if isinstance(stored, Mapping) and isinstance(stored.get("policyObject"), Mapping):
                    stored = stored["policyObject"]
                if self._scope_key(stored) != scope_key:
                    continue
                if body is not None and _same(stored) != _same(dict(body)):
                    continue
                return str(pid)
        except Exception:  # noqa: BLE001 - an unreadable producer leaves the id unknown
            return None
        return None

    def _adopt_lost_create(self, transaction_id: str) -> Optional[str]:
        """Bind the policy a lost APPLY created, found at the producer, or ``None``."""
        record = self.durable_binding(transaction_id)
        found = None if record is None else self._unnamed_policy_on(record.scope_key)
        if found is None:
            return None
        self._bindings[transaction_id] = found
        self._declare_policy_type(found)
        self._bind(transaction_id, found, BindingState.BOUND,
                   detail=f"policy {found} the lost APPLY created, found at the producer")
        return found

    def _journal_revision(self, transaction_id: str, body: Mapping[str, Any]) -> None:
        """Record *body*'s revision before it is sent, as ``_reserve`` does for APPLY.

        Q-3: a baseline rewrite whose answer was lost had already stored its
        revision at the producer, and the retry drafted the same one again ("policy
        update requires a newer revision").  Journalled first, the retry drafts
        above it whether or not the lost write landed.
        """
        revision = self.drafted_revision(body)
        current = None if self._journal is None else self._journal.binding_for(transaction_id)
        if current is not None and revision is not None and revision > current.policy_revision:
            self._journal.write(current.evolve(policy_revision=revision,
                                               updated_at=self._clock()))

    def _restore_by_handover(
        self, operation: GatewayOperation, token: KernelToken,
        command: Mapping[str, Any], reference: str, bound: str,
    ) -> Optional[GatewayResult]:
        """Hand a steered UE back before withdrawing its policy, or ``None``.

        ``None`` means the ordinary DELETE below is right: this is not a
        steering adapter, the command names no baseline, or the UE was pinned
        where it already belonged.  Otherwise the bound policy is updated to
        pin the baseline cell, and the contracted readback -- which corroborates
        the policy status with the KPM serving cell and polls to its deadline --
        must show the baseline before the DELETE is allowed to follow.  Without
        that observation nothing is withdrawn and the result says so; the
        gateway's own confirmation read then judges the rollback, so a UE that
        never came back is still PARTIAL_APPLY (2026-09-19 boards 093728 and
        110539: DELETE left ue1 on 12345678 and both trials locked down).
        """
        transaction_id = token.transaction_id
        baseline = command.get("value")
        if not self.restore_by_handover or baseline is None or not command.get("axis"):
            return None
        applied = self.__dict__.setdefault("_applied_values", {}).get(transaction_id)
        if applied is None or applied == _same(baseline):
            return None
        # Keyed per operation (2026-09-27 board 809): ue3 hit radio-link failure on the target
        # cell as the HALT hand-back went out, re-established there 29 s later, and the
        # REVERSE_ROLLBACK only re-read -- the one command was lost and the trial locked down.
        # The rollback writes the hand-back again when the HALT's was never observed; that is
        # one UPDATE within the step's four-call bound (``R1_UNDO_STEP_BOUND_MS``).
        written = self.__dict__.setdefault("_restores_written", {})
        written_key = (transaction_id, operation.value)
        if written.get(written_key) != _same(baseline):
            body = self._build_body(dict(command), token)
            rewrite = any(key[0] == transaction_id and key != written_key for key in written)
            if rewrite and self._journal is None and self._builder_takes_last_revision:
                # 2026-09-27 23:1x board 818: the UNDO re-write went out one revision above the
                # HALT's own draft and the producer refused it AIC_STALE_REVISION -- it held a
                # higher one.  A journal-less adapter has no durable seed, so the live policy is it.
                try:
                    live = _trace_of(self._port.get_policy(bound))
                except Exception:  # noqa: BLE001 - unread, the ordinary draft goes out
                    live = {}
                rev, fence = live.get("revision"), live.get("fencingToken")
                if isinstance(rev, int) and not isinstance(rev, bool) and rev >= (self.drafted_revision(body) or 0):
                    kwargs = {"last_revision": rev}
                    if isinstance(fence, int) and not isinstance(fence, bool):
                        kwargs["last_fencing_token"] = fence
                    body = self._build(dict(command), **kwargs)
            if self._scope_moved(bound, body):
                # 2026-09-24 board 151803: the UE re-registered after the steer and
                # the builder follows the role to the new id (board 123728); see
                # ``_replace_policy``.  The fresh policy is withdrawn by the
                # ordinary DELETE below.
                bound = self._replace_policy(
                    transaction_id, bound, body, token=token, reference=reference,
                    detail=(f"handover back: {command['axis']} pinned to its baseline "
                            f"{baseline!r} for {operation.value} on the UE's new id"))
            else:
                self._issue_with_gate_retry(
                    R1PolicyOperation.UPDATE,
                    lambda: self._port.update_policy(bound, dict(body)),
                    token=token, reference=reference, policy_id=bound,
                    detail=(f"handover back: {command['axis']} pinned to its baseline "
                            f"{baseline!r} for {operation.value}"))
                self._remember_fence(bound, body)
            written[written_key] = _same(baseline)
        observed = None if self._readback is None else self._readback(
            scope=command["scope"], transaction_id=transaction_id, policy_id=bound)
        if (self._readback is not None
                and (observed is None
                     or _same(dict(observed).get(command["axis"])) != _same(baseline))):
            # 2026-09-23: the policy-status readback is the only way an *applied*
            # move may be claimed, but proving the **baseline** is a different
            # question -- the one ``_read`` already answers from the independent
            # counter ("the radio did not move", "refused without writing").
            # Measured over 38 hand-backs of 2026-09-22/23 with the KPM stream,
            # uncensored by the readback deadline: 34 UEs were back on their
            # baseline cell (31 within 4.4 s), and 14 of those were never seen
            # through the policy path -- the gateway reported "baseline not
            # observed" and left the policy at the RIC, and the trial locked down.
            # The stream follows the role, so it also sees a UE that re-registered
            # under a new id.  Only the baseline is proven this way; the policy
            # body is never rewritten to another id (the 2026-09-16 invariant).
            independent = self._readback(scope=command["scope"],
                                         transaction_id=transaction_id, policy_id=None)
            if (independent is not None
                    and _same(dict(independent).get(command["axis"])) == _same(baseline)):
                observed = independent
        if observed is None or _same(dict(observed).get(command["axis"])) != _same(baseline):
            return GatewayResult(
                outcome=GatewayOutcome.ACKED,
                evidence_refs=(reference, f"{self.name}:policy:{bound}"),
                detail=(f"handover back to {baseline!r} written; the baseline was not "
                        f"observed, so the policy is not withdrawn"))
        self._applied_values[transaction_id] = _same(baseline)
        return None

    def _baseline_needing_rewrite(
        self, transaction_id: str, policy_id: str,
    ) -> Optional[Tuple[str, Any]]:
        """``(axis, value)`` a withdrawal could not restore, or ``None``.

        ``A1 DELETE`` restores the snapshot the actuator took when the policy
        was created, so it can only ever reach the *creating* transaction's
        baseline.  Every other record for the same policy id belongs to a
        transaction that adopted it, and its own baseline is whatever was live
        when *it* validated.  Where the two agree -- the ordinary case, and all
        thirty-six reversals that succeeded on 2026-09-16/17 -- a withdrawal is
        exactly right and this returns ``None``.

        Only a disagreement needs a write, and only a single-axis one is
        answered here: these adapters hold one axis per policy (the scope key is
        derived from the body), so a baseline naming more than one axis means
        something changed that this method was not written for, and refusing to
        guess is better than restoring the wrong half.
        """
        if self._journal is None:
            return None
        mine = self._journal.binding_for(transaction_id)
        if mine is None or not mine.baseline_config:
            return None
        creator = self._creating_record(policy_id, transaction_id)
        if creator is None:
            return None
        if dict(creator.baseline_config) == dict(mine.baseline_config):
            return None
        if len(mine.baseline_config) != 1:
            return None
        (axis, value), = dict(mine.baseline_config).items()
        return axis, value

    def _creating_record(
        self, policy_id: str, transaction_id: str,
    ) -> Optional[R1BindingRecord]:
        """The record of the transaction that created *policy_id*.

        The lowest revision wins: the creator wrote revision one and every
        adopter wrote above it (``_last_revision`` seeds above, never level).
        """
        best: Optional[R1BindingRecord] = None
        for key in self._journal.transaction_ids():
            record = self._journal.binding_for(key)
            if record is None or record.policy_id != policy_id:
                continue
            if key == transaction_id and best is not None:
                continue
            if best is None or record.policy_revision < best.policy_revision:
                best = record
        if best is not None and best.transaction_id == transaction_id:
            return None
        return best

    def _revision_of_policy(self, policy_id: str) -> Optional[int]:
        """The A1 revision the journal last recorded for this policy id."""
        if self._journal is None:
            return None
        best: Optional[int] = None
        for key in self._journal.transaction_ids():
            record = self._journal.binding_for(key)
            if record is not None and record.policy_id == policy_id:
                value = record.policy_revision
                best = value if best is None else max(best, value)
        return best

    def _mark_finalized(self, transaction_id: str) -> None:
        """Record that the creating trial settled; the policy stays live."""
        if self._journal is None:
            # No journal (the steering adapters): remember it in memory, for this adapter's
            # own lifetime only, so the next transaction on the same UE can adopt it.
            self.__dict__.setdefault("_finalized_live", set()).add(transaction_id)
            return
        current = self._journal.binding_for(transaction_id)
        if current is None or current.finalized:
            return
        self._journal.write(current.evolve(
            finalized=True, updated_at=self._clock(),
            detail="finalized live; the scope may be taken over by a later transaction",
        ))

    def _adopt_own_finalized(self, transaction_id: str, value: Any = None) -> Optional[str]:
        """Journal-less adapters (steering): adopt the policy an earlier, finalized transaction of
        *this* adapter left live, so the APPLY becomes an update of it.

        2026-09-27 board 807 (v5.2): trial 1 steered ue2 to gnb2 and settled SUCCESS, so the policy
        stayed live as retention means; trials 2 and 3 set ue2 back and each CREATE was refused 409
        (one policy per scope) -- two PARTIAL_APPLYs, EXECUTION_FAILURE.  Steering adapters have no
        binding journal (a steering policy must not outlive its composition), so the journal
        takeover never saw it; 7 of ~150 boards in two days hit the same 409.  One steering adapter
        serves one UE, so any live policy it created is on the same scope.  A steering DELETE never
        steers back, so the adopter's own rollback (hand back, then withdraw) is unchanged.
        """
        if self._journal is not None:
            return None
        live = self.__dict__.get("_finalized_live") or set()
        owners = [tx for tx in sorted(live) if tx != transaction_id and tx in self._bindings]
        if not owners:
            return None
        owner = owners[-1]
        policy_id = self._bindings.pop(owner)
        live.discard(owner)
        for other in owners[:-1]:          # older ones cannot still hold the same scope
            live.discard(other)
        self._bindings[transaction_id] = policy_id
        # (Codex) Recorded before the UPDATE leaves: if it lands and its answer is lost, the
        # rollback must still hand the UE back instead of withdrawing with the UE moved.
        if value is not None:
            self.__dict__.setdefault("_applied_values", {})[transaction_id] = _same(value)
        return policy_id

    def _adopt_takeable_policy(
        self, transaction_id: str, body: Optional[Mapping[str, Any]] = None,
        *, scope_key: Optional[str] = None,
    ) -> Optional[str]:
        """Adopt the policy a finished transaction left live on this scope.

        Without this a second trial on the same axis had to CREATE, the
        producer answered 409 (one policy per scope), and the axis was frozen
        for the rest of the episode -- the whole of 2026-09-16's
        CATALOG_EXHAUSTED endings.  Adoption writes the *same* policy id, so
        the producer takes it as an update and the invariant holds.  The
        adopter also inherits the baseline the original recorded, so its own
        rollback restores what was live before it, not the sentinel.
        """
        if self._journal is None or self._scope_key is None:
            return None
        # APPLY derives the scope from the body it is about to send.  A
        # withdrawal has no body, so it uses the scope this transaction already
        # reserved at VALIDATE -- the same scope, named by the record instead of
        # by a document.
        if scope_key is None and body is not None:
            scope_key = self._scope_key(body)
        if scope_key is None:
            reserved = self._journal.binding_for(transaction_id)
            scope_key = None if reserved is None else reserved.scope_key
        if scope_key is None:
            return None
        owner = self._takeable_owner(transaction_id, scope_key)
        if owner is None:
            return None
        policy_id = str(owner.policy_id)
        self._journal.write(owner.evolve(
            state=BindingState.RESTORED, updated_at=self._clock(),
            detail=f"scope taken over by transaction {transaction_id}; policy {policy_id} stays live",
        ))
        current = self._journal.binding_for(transaction_id)
        if current is not None and not current.baseline_config:
            self._journal.write(current.evolve(
                baseline_config=dict(owner.baseline_config), updated_at=self._clock(),
                detail=f"baseline inherited from transaction {owner.transaction_id}",
            ))
        self._bindings[transaction_id] = policy_id
        self._declare_policy_type(policy_id)
        return policy_id

    def _other_holder(
        self, transaction_id: str, scope_key: str,
    ) -> Optional[R1BindingRecord]:
        """A transaction other than this one still holding ``(type, scope)``."""
        for other in self._journal.transaction_ids():
            if other == transaction_id:
                continue
            record = self._journal.binding_for(other)
            if (record is not None and record.holds_scope
                    and record.policy_type_id == self._policy_type_id
                    and record.scope_key == scope_key):
                return record
        return None

    def _takeable_owner(
        self, transaction_id: str, scope_key: str,
    ) -> Optional[R1BindingRecord]:
        """The finished binding whose policy ``transaction_id`` may adopt.

        Deliberately not :meth:`scope_owner`.  APPLY journals *this*
        transaction's own ``RESERVED`` record before it asks whether there is
        anything to adopt, and ``RESERVED`` holds the scope, so at that instant
        two records hold it and ``scope_owner`` returns whichever transaction id
        sorts first.  Inside one case that ordering happened to be harmless --
        ``:trial:4`` sorts after ``:trial:3`` -- but the retention trial's id
        carries a ``/retention`` segment and ``/`` sorts before ``:``, so the
        retention transaction found *itself*, declined to adopt, CREATEd, and
        the producer answered 409 "target scope already owned".  That is the
        whole of 2026-09-17's ``qualified=False ... PARTIAL_APPLY`` retentions.
        Ask for what adoption actually needs instead of a holder: a takeable
        record on this (type, scope) that is not us, highest revision first so
        the answer does not depend on how ids sort.
        """
        best: Optional[R1BindingRecord] = None
        for other in self._journal.transaction_ids():
            if other == transaction_id:
                continue
            record = self._journal.binding_for(other)
            if (record is not None and record.takeable
                    and record.policy_type_id == self._policy_type_id
                    and record.scope_key == scope_key
                    and (best is None or record.policy_revision > best.policy_revision)):
                best = record
        return best

    def _declare_policy_type(self, policy_id: str) -> None:
        """Tell the port which type a policy id this adapter did not create is.

        ``R1Client`` learns a policy's type when it creates it and otherwise
        falls back to the steering type, which would validate a cap or priority
        body against the wrong schema.  Both paths that produce a bound id
        without a create reach here: a binding restored from the journal after
        a restart, and a scope taken over from a finished transaction.  The
        call is optional so any other port stays a plain ``R1PolicyPort``.
        """
        declare = getattr(self._port, "declare_policy_type", None)
        if callable(declare):
            declare(policy_id, self._policy_type_id)

    def _release(self, transaction_id: str, *, detail: str) -> None:
        if self._journal is None:
            return
        current = self._journal.binding_for(transaction_id)
        if current is None:
            return
        self._journal.write(
            current.evolve(
                state=BindingState.RESTORED, updated_at=self._clock(), detail=detail
            )
        )

    def release_unapplied(self, transaction_id: str) -> bool:
        """Free this transaction's reservation if it provably created nothing.

        Called by the gateway for a participant a rollback does not reverse.  A
        reservation taken at VALIDATE, or one whose APPLY was refused before
        anything was created, is released; one whose APPLY may have written
        (``APPLY_IN_FLIGHT``) or that names a policy is left held -- fail-closed.
        """
        if transaction_id in self._bindings or self._journal is None:
            return False
        return self._release_reservation(transaction_id)

    def _apply_in_flight(self, transaction_id: str) -> bool:
        """True when an APPLY left and never came back (its create may have landed).

        ``APPLY_IN_FLIGHT`` is written by APPLY's reservation and overwritten by
        ``_bind`` on success and ``_mark_refused`` on refusal, so only a lost
        answer (or a crash) leaves it standing."""
        record = self.durable_binding(transaction_id)
        return record is not None and record.detail == "APPLY_IN_FLIGHT"

    def _mark_refused(self, transaction_id: str) -> None:
        """An APPLY refused before creation leaves a reservation that created nothing."""
        if self._journal is None or transaction_id in self._bindings:
            return
        current = self._journal.binding_for(transaction_id)
        if current is not None and current.policy_id is None:
            self._journal.write(current.evolve(
                updated_at=self._clock(), detail="APPLY refused; nothing created"))

    def _release_reservation(self, transaction_id: str) -> bool:
        """Release a reservation that never became a policy.  Returns whether one was.

        Only a :class:`~assurance.gateway.r1_binding_journal.BindingState.RESERVED`
        record with no policy id is released here: a bound or restore-pending
        record names a real policy and is unwound through the DELETE path, not
        dropped.  Freeing a dead reservation cannot restore anything -- nothing
        was created -- so it is safe on any terminal, and it is what makes the
        scope reusable by the next trial of a many-trial case.
        """
        if self._journal is None:
            return False
        current = self._journal.binding_for(transaction_id)
        if current is None or current.state is not BindingState.RESERVED or current.policy_id:
            return False
        if current.detail == "APPLY_IN_FLIGHT":
            # A create may have landed; releasing would orphan it (fail-closed).
            return False
        self._release(transaction_id, detail="reservation released; no policy was created")
        return True

    def _confirm_restore(
        self, transaction_id: str, observed: Mapping[str, Any], detail: str
    ) -> str:
        """Release a held binding only when the baseline is what was read back.

        A DELETE response is not recovery, but an acknowledged withdrawal is
        required before a scalar can confirm it. While the binding stands in
        ``RESTORE_PENDING`` the scope is blocked; only that ACK plus observation
        of the recorded pre-policy configuration -- uncapped ``0`` included --
        releases it.
        """
        if self._journal is None:
            return detail
        current = self._journal.binding_for(transaction_id)
        if current is None or current.state is not BindingState.RESTORE_PENDING:
            return detail
        if not current.withdrawal_acknowledged:
            return f"{detail}; withdrawal unconfirmed, the scope stays held"
        if dict(current.baseline_config) != dict(observed):
            return (
                f"{detail}; the baseline has not come back, the scope stays held"
            )
        self._bindings.pop(transaction_id, None)
        self._release(transaction_id, detail="baseline read back; scope released")
        return f"{detail}; baseline restored and the scope released"
