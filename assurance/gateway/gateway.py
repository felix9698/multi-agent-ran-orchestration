"""The token-bound Write Gateway.

Owner lane: **KGW**.  Implements the frozen
:class:`~assurance.gateway.write_gateway.WriteGateway` protocol.

Design section 4.4 makes this the only component that performs dynamic
equipment changes, and it accepts nothing but a Kernel-issued permit.  Four
decisions below are worth reading before the code.

**The permit is the only input after prepare.**  ``prepare`` takes a plan;
every other operation takes a token and nothing else.  The staged plan is
addressed by ``token.transaction_id``, so there is no second door through which
a different payload, a different adapter or a different scope can arrive --
GATE1-MAP's GAP-01 closed structurally rather than by review.  The adapters
themselves live in a private registry built in ``__init__``; the gateway
exposes their *names and paths* for the boundary test and never the objects.

**The five checks and where the sixth answer comes from.**  The frozen
docstring fixes the order: authorisation, lease, fence, observed configuration,
idempotency.  This implementation runs authorisation, lease, fence, the
trial binding, then the idempotency ledger, then the configuration check, and
the ordering of the last two is deliberate:

    A retransmitted command carries the *same* expected configuration hash as
    the original.  Once the original applied, the live configuration no longer
    matches it -- so checking the configuration first would answer a genuine
    retransmission with ``REJECTED_CONFIG_MISMATCH`` and lose the very fact the
    idempotency key exists to record.  Both checks still complete before
    anything is dispatched, so nothing reaches the equipment either way; what
    changes is only which true statement the Kernel is told.

Every refusal is a returned record, never a silent drop:
:meth:`refusals` keeps them and every dispatch lands in :meth:`evidence`.  The
two cases that raise :class:`~assurance.gateway.write_gateway.GatewayRefusal`
instead are the ones where the request never became a command at all -- a
permit for a different operation, and an adapter that is not registered.  Where
:class:`~assurance.gateway.write_gateway.GatewayOutcome` has a dedicated member
for a condition, that member is used and nothing is raised.

Time and identity are injected.  ``clock`` supplies the canonical UTC instant
used for lease expiry and record stamps; there is no random component anywhere
in the gateway, so the same permits over the same adapter produce the same
evidence references.
"""

from __future__ import annotations

import time

from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from assurance.contracts.capability import ActuatorPath
from assurance.core.timebase import parse_utc, utc_now_text
from assurance.gateway.commands import GatewayOperation, build_command, command_hash
from assurance.gateway.journal import (
    UNCERTAIN_PHASES,
    InMemoryTransactionJournal,
    NextSafeAction,
    TransactionJournal,
    TransactionPhase,
    TransactionRecord,
)
from assurance.gateway.plan import ActuationPlan, PlanError, config_hash, frozen_config
from assurance.gateway.registry import GatewayAdapterRegistry
from assurance.gateway.token import KernelToken, TokenKind
from assurance.gateway.write_gateway import (
    HOSTS_WATCHDOGS_ATTRIBUTE,
    GatewayOutcome,
    GatewayRefusal,
    GatewayResult,
    WriteGatewayAdapter,
)

__all__ = ["WATCHDOG_HOSTING_PREFIX", "TokenBoundWriteGateway"]

#: Evidence-reference prefix stating where this transaction's contract
#: watchdogs are armed.  ``:adapter`` means the deployment holds them and the
#: gateway armed them; ``:kernel`` means the deployment cannot and the Kernel
#: must arm them on its own mechanisms.
WATCHDOG_HOSTING_PREFIX = "watchdog-hosting"

#: A reversal's confirming read is retried this many times in all, this far apart, while the
#: permit still covers another wait plus one read at the R1 call bound (board 860).
REVERSAL_CONFIRM_READS = 4
REVERSAL_CONFIRM_WAIT_S = 5.0
R1_READ_BOUND_S = 20.0


def _lease_left_s(token: KernelToken, now: str) -> float:
    try:
        return (parse_utc(token.lease_expiry) - parse_utc(now)).total_seconds()
    except (TypeError, ValueError):
        return 0.0

#: Phases in which nothing is outstanding at the equipment, so a watchdog has
#: nothing left to protect.
_WATCHDOG_SETTLED = frozenset(
    {
        TransactionPhase.FINALIZED,
        TransactionPhase.ROLLED_BACK,
        TransactionPhase.SAFE_STATE,
        TransactionPhase.REFUSED,
    }
)

#: Phases from which a reverse rollback is meaningful.  ``PREPARED``/``READY``
#: are absent on purpose: nothing was applied, so there is nothing to reverse
#: and the trial ends in a pre-commit abort instead (design section 7).
_ROLLBACKABLE = frozenset(
    {
        TransactionPhase.APPLYING,
        TransactionPhase.APPLIED,
        TransactionPhase.PARTIALLY_APPLIED,
        TransactionPhase.UNCERTAIN,
        TransactionPhase.REFUSED,
        TransactionPhase.STOPPED,
        TransactionPhase.ROLLING_BACK,
        TransactionPhase.ROLLBACK_INCOMPLETE,
    }
)



def _applied_basis(matched, acked):
    """What the applied-axis count rests on.  An ACK is not an observation."""
    if matched is not None and matched >= acked:
        return "are live (confirmed by readback)"
    if matched:
        return ("were acknowledged (the readback confirmed only %d)" % matched)
    return "were acknowledged (the readback confirmed none)"


class TokenBoundWriteGateway:
    """The only component that performs dynamic equipment changes.

    Parameters
    ----------
    adapters:
        Name to adapter.  Registered through
        :class:`~assurance.gateway.registry.GatewayAdapterRegistry`, so a
        ``LAB_SETUP_PREPARATION`` adapter is refused here and never becomes
        reachable (design section 9).
    safe_state:
        The deployment's *complete* contracted safe configuration -- every axis
        of the configuration surface, with the value the deployment is safe at.
        Emergency safe state must work from any state including one where
        prepare never completed, which is only possible if the safe
        configuration is known without a transaction.
    journal:
        Durable transaction record.  Defaults to an in-memory journal; a live
        deployment passes
        :class:`~assurance.gateway.journal.JsonFileTransactionJournal`.
    axis_adapters:
        Which registered adapter owns which *configuration axis*.  Empty is the
        ordinary single-participant deployment: one client sees the whole
        surface and every command goes to the transaction's own adapter, which
        is exactly what this gateway did before the field existed.  A
        composition that reaches the deployment over more than one client --
        a PRIMARY steering policy over ``r1`` and a SUPPLEMENTARY UE cap over
        ``r1-cap`` -- declares the ownership here, because no single client can
        read back an axis it does not serve and merging two partial
        observations is the only honest way to get the surface digest.

        It is a *deployment* fact, fixed at construction.  A plan step may
        carry the same name (``PlanStep.adapter``) and the two must agree; a
        step naming a different owner than the deployment declared is refused
        at prepare rather than dispatched somewhere nobody contracted.
    clock:
        Returns the current instant in canonical UTC form.
    """

    def __init__(
        self,
        *,
        adapters: Mapping[str, WriteGatewayAdapter],
        safe_state: Mapping[str, Any],
        journal: Optional[TransactionJournal] = None,
        axis_adapters: Optional[Mapping[str, str]] = None,
        clock: Callable[[], str] = utc_now_text,
    ) -> None:
        self._safe_state = frozen_config(safe_state)
        self._safe_state_hash = config_hash(self._safe_state)
        self._surface = frozenset(self._safe_state)
        self._registry = GatewayAdapterRegistry()
        for name, adapter in sorted(dict(adapters).items()):
            self._registry.register(name, adapter)
        if not self._registry.registered_paths():
            raise GatewayRefusal("a gateway with no registered adapter cannot act")
        registered = set(self._registry.registered_paths())
        self._axis_adapters: Dict[str, str] = {}
        for axis, name in dict(axis_adapters or {}).items():
            if axis not in self._surface:
                raise GatewayRefusal(
                    f"axis {axis!r} is not on the contracted configuration surface"
                )
            if name not in registered:
                raise GatewayRefusal(
                    f"axis {axis!r} is declared on unregistered adapter {name!r}"
                )
            self._axis_adapters[str(axis)] = str(name)
        self._journal: TransactionJournal = (
            journal if journal is not None else InMemoryTransactionJournal()
        )
        self._clock = clock
        self._sleep: Callable[[float], None] = time.sleep   # tests replace it
        self._evidence: Dict[str, Dict[str, Any]] = {}
        self._refusals: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # introspection (not part of the frozen protocol)
    # ------------------------------------------------------------------ #

    def registered_paths(self) -> Mapping[str, ActuatorPath]:
        """Adapter name to actuator path.  Names only -- never the adapters."""
        return self._registry.registered_paths()

    def transaction_record(self, transaction_id: str) -> Optional[TransactionRecord]:
        """The durable record, for the Kernel's recovery sweep."""
        return self._journal.read(transaction_id)

    def uncertain_transactions(self) -> Tuple[str, ...]:
        """Transactions that must be resolved before another trial may start.

        Design section 8: restart recovery blocks new trials until every
        uncertain transaction is queried and safely aborted, finalized, rolled
        back or placed in incident lockdown.
        """
        return tuple(
            transaction_id
            for transaction_id in self._journal.transaction_ids()
            if (record := self._journal.read(transaction_id)) is not None
            and record.is_uncertain
        )

    def evidence(self, reference: str) -> Mapping[str, Any]:
        """The raw request/response record one evidence reference points at."""
        return dict(self._evidence[reference])

    def evidence_references(self) -> Tuple[str, ...]:
        """Every evidence reference this gateway has produced, in order."""
        return tuple(self._evidence)

    def refusals(self) -> Tuple[Mapping[str, Any], ...]:
        """Every refusal, in order.  A refused command is recorded, not dropped."""
        return tuple(dict(entry) for entry in self._refusals)

    # ------------------------------------------------------------------ #
    # the frozen protocol
    # ------------------------------------------------------------------ #

    def prepare(self, *, token: KernelToken, plan: Mapping[str, Any]) -> GatewayResult:
        """Validate and stage *plan* without any side effect."""
        record, refused = self._guard(token, TokenKind.PREPARE)
        if refused is not None:
            return refused
        if record is not None and record.phase not in (
            TransactionPhase.PREPARED,
            TransactionPhase.READY,
        ):
            # Re-staging over a transaction that is past the commit line would
            # overwrite the record of what is live with a plan that assumes
            # nothing is.  A retry after an apply is a new transaction, or it
            # is a recovery.
            return self._reject(
                token,
                record,
                GatewayOutcome.REJECTED,
                f"a change may already exist for this transaction ({record.phase.value}); "
                "recover it before staging another",
            )
        try:
            staged = ActuationPlan.from_mapping(plan)
        except PlanError as exc:
            return self._reject(
                token, record, GatewayOutcome.REJECTED, f"plan is not admissible: {exc}"
            )
        if staged.surface != self._surface:
            return self._reject(
                token,
                record,
                GatewayOutcome.REJECTED,
                "plan describes a different configuration surface than the deployment's",
            )
        self._registry.resolve(staged.adapter)
        participants = self._plan_participants(staged)
        for name in participants:
            # Resolving every participant here is the boundary check: a plan
            # naming a client this gateway never registered is refused before
            # anything is staged, not discovered at the first write.
            self._registry.resolve(name)
        conflicting = [
            step.axis for step in staged.steps
            if step.adapter is not None
            and step.axis in self._axis_adapters
            and self._axis_adapters[step.axis] != step.adapter
        ]
        if conflicting:
            return self._reject(
                token,
                record,
                GatewayOutcome.REJECTED,
                "plan steps name a different adapter than the deployment declares "
                f"for {conflicting}",
            )
        if staged.baseline_hash != token.expected_config_hash:
            return self._reject(
                token,
                record,
                GatewayOutcome.REJECTED_CONFIG_MISMATCH,
                "the plan's baseline is not the configuration the permit names",
            )
        observed, refs = self._observe(participants, token, staged.scope, plan=staged)
        if observed is None:
            return self._reject(
                token,
                record,
                GatewayOutcome.UNKNOWN,
                "the configuration could not be read; nothing was staged" + self._why_unread(),
                refs,
            )
        if observed != token.expected_config_hash:
            return self._reject(
                token,
                record,
                GatewayOutcome.REJECTED_CONFIG_MISMATCH,
                "the live configuration is not the one the permit names",
                refs,
                observed=observed,
            )
        for index, step in enumerate(staged.steps):
            # Prepare-all-before-write: every participant validates its own step
            # before any of them writes, so a composition cannot get a PRIMARY
            # policy created and then discover the SUPPLEMENTARY one is
            # inadmissible.
            validation, validation_refs = self._dispatch(
                self._registry.resolve(self._step_adapter_name(staged, step.axis)),
                token,
                GatewayOperation.VALIDATE,
                scope=staged.scope,
                axis=step.axis,
                value=step.value,
                index=index,
            )
            refs = refs + validation_refs
            if validation.outcome is not GatewayOutcome.ACKED:
                return self._reject(
                    token,
                    record,
                    GatewayOutcome.REJECTED,
                    f"prepare was refused downstream on {step.axis}: {validation.detail}"[:200],
                    refs,
                    observed=observed,
                )
        staged_record = TransactionRecord(
            transaction_id=token.transaction_id,
            trial_id=token.trial_id,
            phase=TransactionPhase.PREPARED,
            adapter=staged.adapter,
            baseline_config_hash=staged.baseline_hash,
            applied_config_hash=staged.applied_hash,
            plan=staged.to_canonical_dict(),
            last_token=token.to_canonical_dict(),
            next_safe_action=NextSafeAction.READY,
            observed_config_hash=observed,
            participants=participants,
            reservations={"adapter": staged.adapter, "planHash": staged.content_hash()},
            # Carried across a re-prepare of the same transaction: dropping the
            # effect ledger would let an idempotency key that already produced
            # an effect be reused as if it were fresh.
            effects={key: dict(value) for key, value in record.effects.items()}
            if record is not None
            else {},
            evidence_refs=record.evidence_refs if record is not None else (),
            updated_at=self._clock(),
        )
        return self._settle(
            token,
            staged_record,
            GatewayOutcome.ACKED,
            "staged; nothing was written",
            refs,
            observed=observed,
        )

    def ready(self, *, token: KernelToken) -> GatewayResult:
        """Confirm durable ready state."""
        record, refused = self._guard(token, TokenKind.READY)
        if refused is not None:
            return refused
        if record is None:
            return self._reject(
                token, record, GatewayOutcome.REJECTED, "no staged plan for this transaction"
            )
        if record.phase not in (TransactionPhase.PREPARED, TransactionPhase.READY):
            return self._reject(
                token,
                record,
                GatewayOutcome.REJECTED,
                f"ready is not available from {record.phase.value}",
            )
        staged = ActuationPlan.from_mapping(record.plan)
        adapter = self._registry.resolve(record.adapter)
        participants = self._plan_participants(staged)
        observed, refs = self._observe(
            self._read_participants(record.adapter), token, staged.scope, plan=staged
        )
        if observed is None:
            return self._reject(
                token, record, GatewayOutcome.UNKNOWN,
                "the configuration could not be read" + self._why_unread(), refs
            )
        if observed != token.expected_config_hash:
            return self._reject(
                token,
                record,
                GatewayOutcome.REJECTED_CONFIG_MISMATCH,
                "the configuration moved between prepare and ready",
                refs,
                observed=observed,
            )
        # "All-component READY" (design section 7 step 6) includes the
        # contracted guards.  Where they are armed depends on what the
        # deployment can carry, and only the adapter knows that, so the
        # adapter declares it (SEAMS-GATE2.md section 8.3, judgement 2).  The
        # declaration is reported either way: the Kernel must be able to tell
        # "this adapter cannot host, arm them yourself" from "this adapter can
        # host and said nothing", and silence cannot mean both.
        # A composition hosts its guards only if *every* participant does: a
        # guard armed on one client says nothing about a change the other one
        # carries, and losing strength is the safe direction to be wrong in.
        hosting = all(
            bool(getattr(self._registry.resolve(name), HOSTS_WATCHDOGS_ATTRIBUTE, False))
            for name in participants
        )
        refs = refs + (
            f"{WATCHDOG_HOSTING_PREFIX}:{record.adapter}:"
            f"{'adapter' if hosting else 'kernel'}",
        )
        if hosting:
            for position, watchdog_id in enumerate(staged.watchdogs):
                armed, arm_refs = self._dispatch(
                    adapter,
                    token,
                    GatewayOperation.ARM_WATCHDOG,
                    scope=staged.scope,
                    watchdog_id=watchdog_id,
                    index=position + 1,
                )
                refs = refs + arm_refs
                if armed.outcome is not GatewayOutcome.ACKED:
                    # An adapter that claims it can host and then will not is a
                    # failed readiness, not an unguarded commit.
                    return self._reject(
                        token,
                        record,
                        GatewayOutcome.REJECTED,
                        f"watchdog {watchdog_id} could not be armed: {armed.detail}"[:200],
                        refs,
                        observed=observed,
                    )
        return self._settle(
            token,
            record.evolve(
                phase=TransactionPhase.READY,
                next_safe_action=NextSafeAction.COMMIT,
                participants=participants,
                observed_config_hash=observed,
            ),
            GatewayOutcome.ACKED,
            "durably ready; guards armed; still nothing written",
            refs,
            observed=observed,
        )

    def commit(self, *, token: KernelToken) -> GatewayResult:
        """Apply the staged change through the official O-RAN path."""
        record, refused = self._guard(token, TokenKind.COMMIT)
        if refused is not None:
            return refused
        if record is None:
            return self._reject(
                token, record, GatewayOutcome.REJECTED, "no staged plan for this transaction"
            )
        if record.phase is not TransactionPhase.READY:
            return self._reject(
                token,
                record,
                GatewayOutcome.REJECTED,
                f"commit needs a durable READY, found {record.phase.value}",
            )
        staged = ActuationPlan.from_mapping(record.plan)
        participants = self._plan_participants(staged)
        observed, refs = self._observe(
            self._read_participants(record.adapter), token, staged.scope, plan=staged
        )
        if observed is None:
            return self._reject(
                token,
                record,
                GatewayOutcome.UNKNOWN,
                "the configuration could not be read; nothing was applied" + self._why_unread(),
                refs,
            )
        if observed != token.expected_config_hash:
            return self._reject(
                token,
                record,
                GatewayOutcome.REJECTED_CONFIG_MISMATCH,
                "the live configuration is not the one the permit names",
                refs,
                observed=observed,
            )
        # Durable before the first write leaves the gateway: a process that
        # dies mid-apply must be found as uncertain, never as absent.
        self._journal.write(
            record.evolve(
                phase=TransactionPhase.APPLYING,
                last_token=token.to_canonical_dict(),
                next_safe_action=NextSafeAction.RECOVERY_CONFIRM,
                participants=participants,
                watchdog_deadline=token.lease_expiry,
                updated_at=self._clock(),
            )
        )
        acked = 0
        #: ACKs for steps that **move** an axis off its baseline -- a real write the
        #: producer accepted.  ``acked`` also counts the no-op steps of a joint plan,
        #: which is why it cannot tell "nothing was accepted" apart (see below).
        moved_acked = 0
        lost_ack = False
        failure = ""
        for index, step in enumerate(staged.steps):
            # Ordered multi-participant commit: PRIMARY first, in plan order,
            # each write on the client the staged plan permanently bound it to.
            result, step_refs = self._dispatch(
                self._registry.resolve(self._step_adapter_name(staged, step.axis)),
                token,
                GatewayOperation.APPLY,
                scope=staged.scope,
                axis=step.axis,
                value=step.value,
                index=index,
            )
            refs = refs + step_refs
            if result.outcome is GatewayOutcome.ACKED:
                acked += 1
                if str(step.value) != str(staged.baseline_config.get(step.axis)):
                    moved_acked += 1
                continue
            if result.outcome is GatewayOutcome.UNKNOWN:
                lost_ack = True
            failure = f"{step.axis}: {result.outcome.value} {result.detail}".strip()
            break
        return self._reconcile_apply(token, record, staged, acked, lost_ack, failure, refs,
                                     moved_acked=moved_acked)

    def stop(self, *, token: KernelToken) -> GatewayResult:
        """Halt the change immediately."""
        record, refused = self._guard(token, TokenKind.STOP)
        if refused is not None:
            return refused
        if record is None:
            return self._reject(
                token, record, GatewayOutcome.REJECTED, "no durable transaction to halt"
            )
        staged = ActuationPlan.from_mapping(record.plan)
        # No configuration check: a halt that first requires the world to look
        # as expected is exactly the halt that fails when it is needed.  A stop
        # is a safety action and outranks any semantic evaluation (section 7).
        # Every participant is halted, in reverse write order: the SUPPLEMENTARY
        # change comes off before the PRIMARY one it supported.
        participants = tuple(record.participants) or self._plan_participants(staged)
        refs: Tuple[str, ...] = ()
        outcome = GatewayOutcome.ACKED
        for position, name in enumerate(reversed(participants)):
            result, step_refs = self._dispatch(
                self._registry.resolve(name),
                token,
                GatewayOperation.HALT,
                scope=staged.scope,
                index=position,
                reference_suffix=("" if len(participants) == 1 else f":{name}"),
            )
            refs = refs + step_refs
            if result.outcome not in (GatewayOutcome.ACKED, GatewayOutcome.ALREADY_APPLIED):
                outcome = result.outcome
        following = (
            NextSafeAction.REVERSE_ROLLBACK
            if record.applied_axes or record.phase in UNCERTAIN_PHASES
            else NextSafeAction.SETTLE
        )
        # No observation is reported: a halt deliberately does not read, and a
        # digest from before the halt is not "what the gateway read back".
        return self._settle(
            token,
            record.evolve(phase=TransactionPhase.STOPPED, next_safe_action=following),
            outcome,
            f"halted from {record.phase.value}",
            refs,
        )

    def reverse_rollback(self, *, token: KernelToken) -> GatewayResult:
        """Reverse the applied change in the opposite order it was applied."""
        record, refused = self._guard(token, TokenKind.REVERSE_ROLLBACK)
        if refused is not None:
            return refused
        if record is None:
            return self._reject(
                token, record, GatewayOutcome.REJECTED, "no durable transaction to reverse"
            )
        if record.phase not in _ROLLBACKABLE:
            return self._reject(
                token,
                record,
                GatewayOutcome.REJECTED,
                f"nothing applied to reverse from {record.phase.value}",
            )
        staged = ActuationPlan.from_mapping(record.plan)
        observed, refs = self._observe(
            self._read_participants(record.adapter), token, staged.scope, plan=staged
        )
        # 2026-09-23 (라이브 판 formal38guarded-20260923T060007, codex 감사 R-2):
        # 선행 읽기가 **값을 못 받으면** 여기서 거절하고 있었다 -- UNDO 를 하나도 보내지
        # 않고.  그 판에서는 우리가 만든 A1 조종 정책이 RIC 에 그대로 남은 채 커널이
        # 잠갔다.  읽을 수 없음은 **불확정**이고, 불확정에서의 방향은 아래 주석이 이미
        # 정해 두었다: 계획 전체를 되돌린다(적힌 적 없는 축의 기준선 복원은 명령 하나
        # 값이고 틀려도 안전한 쪽이다).  그래서 되돌림은 보내고, **성공 판정은 여전히
        # 확인 읽기가 한다** -- 확인 못 하면 아래에서 UNKNOWN 이다.
        # 선행 읽기가 **성공했는데** 다른 설정을 말하면 그대로 거절한다.  그건 모르는
        # 설정 위에서 되돌리지 않는다는 펜싱 규칙이고, 이 수정은 그것을 건드리지 않는다.
        unread_before = observed is None
        unread_reason = self._why_unread() if unread_before else ""
        # 2026-09-23: 읽혔는데 허가증과 다르더라도, 그것이 **이 계획의 축들만** 기준값·계획값
        # 사이에서 섞인 상태면 우리 자신의 부분 적용이다 -- A1 효과는 순서 없이 착지해서,
        # 커밋 읽기 뒤 되돌리기 전에 축 하나가 더 들어오는 일이 흔하다.  그 상태에서 계획
        # 전체를 기준값으로 되돌리는 것은 잘 정의돼 있고 안전하다.  이 계획 밖의 값이 하나라도
        # 있으면 여전히 거절한다 (모르는 설정 위에서 되돌리지 않는다는 펜싱 규칙).
        own_other_state = (not unread_before and observed != token.expected_config_hash
                           and observed in staged.subset_hashes())
        if not unread_before and observed != token.expected_config_hash and not own_other_state:
            # After a partial or unknown apply the Kernel learns the live hash
            # from a configuration reread and reissues; a rollback permit that
            # names a configuration nobody observed is refused, not guessed at.
            return self._reject(
                token,
                record,
                GatewayOutcome.REJECTED_CONFIG_MISMATCH,
                "the permit names a configuration that is not live; reread first",
                refs,
                observed=observed,
            )
        self._journal.write(
            record.evolve(
                phase=TransactionPhase.ROLLING_BACK,
                last_token=token.to_canonical_dict(),
                next_safe_action=NextSafeAction.RECOVERY_CONFIRM,
                updated_at=self._clock(),
            )
        )
        # With the phase uncertain and no axis recorded -- a process that died
        # between the durable APPLYING record and the first acknowledgement --
        # reverse the whole plan.  Restoring the baseline value of an axis that
        # was never written costs one command and is the safe direction to be
        # wrong in.
        reversing = record.applied_axes or (
            staged.axes if record.phase in UNCERTAIN_PHASES else ()
        )
        if unread_before or own_other_state:
            # 몇 축까지 갔는지 읽지 못했거나 기록과 다른 조합이 살아 있으므로 기록된 것만
            # 믿지 않는다 -- 계획 전체.
            reversing = staged.axes
        # 2026-09-20: a participant the reversal does not touch -- its APPLY was
        # never sent, or was refused before anything was created -- still holds
        # the scope reservation it took at VALIDATE.  Board 20260919T183101
        # trial 4: cap@ue3's CREATE was refused, the rollback reversed only the
        # written axes, and pfWeight@ue1's reservation blocked trial 5 ("scope is
        # still owned by ... trial:4 (RESERVED)").  The adapter releases only a
        # reservation that provably created nothing; anything else stays held.
        touched = {self._step_adapter_name(staged, axis) for axis in reversing}
        for name in (tuple(record.participants) or self._plan_participants(staged)):
            if name in touched:
                continue
            release = getattr(self._registry.resolve(name), "release_unapplied", None)
            if callable(release):
                release(token.transaction_id)
        undone = 0
        for offset, axis in enumerate(reversed(reversing)):
            # Reverse order across participants too: the last client written is
            # the first unwound, so a composition never passes through a
            # configuration that was never valid.
            result, step_refs = self._dispatch(
                self._registry.resolve(self._step_adapter_name(staged, axis)),
                token,
                GatewayOperation.UNDO,
                scope=staged.scope,
                axis=axis,
                value=staged.baseline_config[axis],
                index=offset,
            )
            refs = refs + step_refs
            if result.outcome is GatewayOutcome.ACKED:
                undone += 1
        confirmed, confirm_refs = self._observe(
            self._read_participants(record.adapter), token, staged.scope,
            index=len(reversing) + 1, plan=staged,
        )
        # 2026-09-28 board 860 trial 3: the reversal handed ue2 back to its cell and re-wrote its
        # weight; the one confirming read ran while the returned UE's identity was not yet fresh
        # ("UE scope has no unique fresh KPM/header") and the trial locked down over a completed
        # reversal.  A read writes nothing: look again a few times inside the permit.
        reads = 1
        # (Codex) _observe reads the participants one after another: price the whole read, and
        # look at the lease again after the wait.
        read_s = R1_READ_BOUND_S * max(1, len(self._read_participants(record.adapter)))
        while (confirmed is None and reads < REVERSAL_CONFIRM_READS
               and _lease_left_s(token, self._clock()) > REVERSAL_CONFIRM_WAIT_S + read_s):
            self._wait_for_the_clock(REVERSAL_CONFIRM_WAIT_S)
            if _lease_left_s(token, self._clock()) <= read_s:
                break
            reads += 1
            confirmed, more = self._observe(
                self._read_participants(record.adapter), token, staged.scope,
                index=len(reversing) + reads, plan=staged,
            )
            confirm_refs = confirm_refs + more
        refs = refs + confirm_refs
        if confirmed is None:
            return self._settle(
                token,
                record.evolve(
                    phase=TransactionPhase.UNCERTAIN,
                    next_safe_action=NextSafeAction.RECOVERY_CONFIRM,
                ),
                GatewayOutcome.UNKNOWN,
                "reversal dispatched; the restored configuration could not be read"
                + (f" (and the configuration could not be read before reversing either"
                   f"{unread_reason})" if unread_before else ""),
                refs,
            )
        if confirmed == record.baseline_config_hash:
            return self._settle(
                token,
                record.evolve(
                    phase=TransactionPhase.ROLLED_BACK,
                    next_safe_action=NextSafeAction.SETTLE,
                    applied_axes=(),
                    observed_config_hash=confirmed,
                    watchdog_deadline=None,
                ),
                GatewayOutcome.ACKED,
                # 여기서 참인 주장은 **독립 되읽기가 기준선을 확인했다**는 것이다.
                # 앞의 수는 ACK 수이지 되돌린 축의 수가 아니므로(아래 실패 분기의
                # 주석 참조) 같은 말로 쓴다.
                f"{undone} of {len(reversing)} reversals acknowledged; "
                f"baseline confirmed by readback"
                + ("; the configuration could not be read before reversing, so the "
                   f"whole plan was reversed{unread_reason}" if unread_before else ""),
                refs,
                observed=confirmed,
            )
        return self._settle(
            token,
            record.evolve(
                phase=TransactionPhase.ROLLBACK_INCOMPLETE,
                next_safe_action=NextSafeAction.EMERGENCY_SAFE_STATE,
                observed_config_hash=confirmed,
            ),
            GatewayOutcome.PARTIAL_APPLY,
            # Name the two hashes.  Twice in thirty live episodes (2026-09-16
            # 222038 and 2026-09-17 004935) this branch fired while the event it
            # produced carried *equal* expected and observed hashes -- because
            # the event records the Kernel's ``expected_config_hash`` and this
            # branch compares the transaction's own ``baseline_config_hash``,
            # which are two different notions of "baseline".  With only the
            # sentence there was no way to tell a genuine unrestored axis from a
            # baseline the record drifted away from, and both ended the case
            # ``RECOVERY_FAILURE`` with retention blocked.
            # ``undone`` counts **acknowledgements, not restorations.**  An
            # adapter ACKs an UNDO it had nothing to withdraw for ("no policy
            # bound to this transaction") and one already in flight, and both
            # land in this count.  So "reversed 8 of 9" can mean eight no-ops.
            # That reading cost an hour on 2026-09-17 and nearly a weakened
            # safety check: seeing ``observed == permit expected`` next to
            # "reversed 8 of 9" invites "the reversal worked, the comparison is
            # wrong", when in fact ``expected_config_hash`` is *what the gateway
            # must observe BEFORE acting* -- so observed equal to it means
            # nothing moved at all.  The sentence now says which is which.
            (f"reversal did not restore the baseline configuration "
             f"({undone} of {len(reversing)} reversals were acknowledged "
             f"-- an acknowledgement is not a restoration; "
             f"observed {str(confirmed)[:12]}, "
             f"transaction baseline {str(record.baseline_config_hash)[:12]}, "
             f"permit expected {str(token.expected_config_hash)[:12]})"),
            refs,
            observed=confirmed,
        )

    def reread_configuration(self, *, token: KernelToken) -> GatewayResult:
        """Read the live configuration back without changing it."""
        record, refused = self._guard(
            token, TokenKind.CONFIGURATION_REREAD, cacheable=False
        )
        if refused is not None:
            return refused
        if record is None:
            return self._reject(
                token, record, GatewayOutcome.REJECTED, "no durable transaction to reread"
            )
        staged = ActuationPlan.from_mapping(record.plan)
        # No configuration check here: learning the live digest is the whole
        # operation, and a reread that refused whenever the answer differed
        # from the Kernel's belief could never report drift.
        observed, refs = self._observe(
            self._read_participants(record.adapter), token, staged.scope, plan=staged
        )
        if observed is None:
            return self._settle(
                token,
                record.evolve(next_safe_action=NextSafeAction.RECOVERY_CONFIRM),
                GatewayOutcome.UNKNOWN,
                "the live configuration could not be read",
                refs,
            )
        detail = (
            "reread matches the permit"
            if observed == token.expected_config_hash
            else "reread differs from the permit"
        )
        return self._settle(
            token,
            record.evolve(observed_config_hash=observed),
            GatewayOutcome.ACKED,
            detail,
            refs,
            observed=observed,
        )

    def confirm_recovery(self, *, token: KernelToken) -> GatewayResult:
        """Confirm the deployment is in a known safe state."""
        record, refused = self._guard(token, TokenKind.RECOVERY_CONFIRM, cacheable=False)
        if refused is not None:
            return refused
        if record is None:
            return self._reject(
                token, record, GatewayOutcome.REJECTED, "no durable transaction to recover"
            )
        staged = ActuationPlan.from_mapping(record.plan)
        observed, refs = self._observe(
            self._read_participants(record.adapter), token, staged.scope, plan=staged
        )
        if observed is None:
            # Fail rather than assume: an unfounded confirmation would release
            # the block that exists to stop the next trial running on top of an
            # unknown configuration (design section 8).
            return self._settle(
                token,
                record.evolve(
                    phase=TransactionPhase.UNCERTAIN,
                    next_safe_action=NextSafeAction.RECOVERY_CONFIRM,
                ),
                GatewayOutcome.UNKNOWN,
                "the configuration could not be read; the transaction stays uncertain" + self._why_unread(),
                refs,
            )
        if observed == record.baseline_config_hash:
            return self._settle(
                token,
                record.evolve(
                    phase=TransactionPhase.ROLLED_BACK,
                    next_safe_action=NextSafeAction.SETTLE,
                    applied_axes=(),
                    observed_config_hash=observed,
                    watchdog_deadline=None,
                ),
                GatewayOutcome.ACKED,
                "baseline is live: the change was reversed or never applied",
                refs,
                observed=observed,
            )
        if observed == record.applied_config_hash:
            return self._settle(
                token,
                record.evolve(
                    phase=TransactionPhase.APPLIED,
                    next_safe_action=NextSafeAction.REVERSE_ROLLBACK,
                    applied_axes=staged.axes,
                    observed_config_hash=observed,
                ),
                GatewayOutcome.ACKED,
                "the applied configuration is live; it is the Kernel's to finalize or reverse",
                refs,
                observed=observed,
            )
        matched = self._largest_prefix(staged.prefix_hashes(), observed)
        return self._settle(
            token,
            record.evolve(
                phase=TransactionPhase.PARTIALLY_APPLIED,
                next_safe_action=NextSafeAction.REVERSE_ROLLBACK,
                applied_axes=staged.axes[: matched if matched is not None else len(staged.axes)],
                observed_config_hash=observed,
            ),
            GatewayOutcome.PARTIAL_APPLY,
            "the live configuration is neither the baseline nor the applied change",
            refs,
            observed=observed,
        )

    def emergency_safe_state(self, *, token: KernelToken) -> GatewayResult:
        """Drive the deployment to its contracted safe state."""
        record, refused = self._guard(token, TokenKind.EMERGENCY_SAFE_STATE)
        if refused is not None:
            return refused
        adapter_name, scope, staged = self._emergency_target(record)
        self._registry.resolve(adapter_name)
        result, _ = self._drive_to_safe_state(token, adapter_name, record, scope, staged)
        return result

    def finalize_live(self, *, token: KernelToken) -> GatewayResult:
        """Make a successful change the live configuration."""
        record, refused = self._guard(token, TokenKind.FINALIZE_LIVE)
        if refused is not None:
            return refused
        if record is None:
            return self._reject(
                token, record, GatewayOutcome.REJECTED, "no durable transaction to finalize"
            )
        if record.phase is not TransactionPhase.APPLIED:
            return self._reject(
                token,
                record,
                GatewayOutcome.REJECTED,
                f"finalize needs an applied change, found {record.phase.value}",
            )
        staged = ActuationPlan.from_mapping(record.plan)
        participants = tuple(record.participants) or self._plan_participants(staged)
        # This read is the contracted configuration reread task section 6.9
        # requires before a success settlement; the finalize permit must name
        # the applied configuration and the equipment must still be in it.
        observed, refs = self._observe(
            self._read_participants(record.adapter), token, staged.scope, plan=staged
        )
        if observed is None:
            return self._reject(
                token, record, GatewayOutcome.UNKNOWN, "the configuration could not be reread", refs
            )
        if observed != token.expected_config_hash or observed != record.applied_config_hash:
            return self._reject(
                token,
                record,
                GatewayOutcome.REJECTED_CONFIG_MISMATCH,
                "the applied configuration is no longer live; no success may be finalized",
                refs,
                observed=observed,
            )
        finalize_outcome = GatewayOutcome.ACKED
        finalize_detail = ""
        for position, name in enumerate(participants):
            step, finalize_refs = self._dispatch(
                self._registry.resolve(name),
                token,
                GatewayOperation.FINALIZE,
                scope=staged.scope,
                index=position,
                reference_suffix=("" if len(participants) == 1 else f":{name}"),
            )
            refs = refs + finalize_refs
            if step.outcome is not GatewayOutcome.ACKED:
                finalize_outcome, finalize_detail = step.outcome, step.detail
                break
        result = GatewayResult(outcome=finalize_outcome, detail=finalize_detail)
        if result.outcome is not GatewayOutcome.ACKED:
            # Without the acknowledgement the trial is not a deployed success,
            # whatever the predicates said (design section 7.9).
            return self._settle(
                token,
                record.evolve(next_safe_action=NextSafeAction.FINALIZE_LIVE),
                GatewayOutcome.UNKNOWN
                if result.outcome is not GatewayOutcome.REJECTED
                else GatewayOutcome.REJECTED,
                f"finalize was not acknowledged: {result.detail}"[:200],
                refs,
                observed=observed,
            )
        return self._settle(
            token,
            record.evolve(
                phase=TransactionPhase.FINALIZED,
                next_safe_action=NextSafeAction.SETTLE,
                observed_config_hash=observed,
                watchdog_deadline=None,
            ),
            GatewayOutcome.ACKED,
            "reread confirmed and finalize acknowledged",
            refs,
            observed=observed,
        )

    def query_transaction(self, transaction_id: str) -> GatewayResult:
        """Report the durable state of *transaction_id*."""
        record = self._journal.read(transaction_id)
        if record is None:
            return GatewayResult(
                outcome=GatewayOutcome.UNKNOWN,
                detail=f"no durable record for {transaction_id}",
            )
        detail = (
            f"phase={record.phase.value} fence={record.last_token.get('fencingToken')} "
            f"applied={list(record.applied_axes)} next={record.next_safe_action.value}"
        )
        return GatewayResult(
            outcome=GatewayOutcome.UNKNOWN if record.is_uncertain else GatewayOutcome.ACKED,
            observed_config_hash=record.observed_config_hash,
            evidence_refs=record.evidence_refs,
            detail=detail,
        )

    # ------------------------------------------------------------------ #
    # watchdog
    # ------------------------------------------------------------------ #

    def watchdog_check(self, now: Optional[str] = None) -> Tuple[GatewayResult, ...]:
        """Drive every transaction whose permit outlived its lease to safe state.

        Task section 6.5 keeps watchdogs armed from apply until recovery
        verification or live finalize.  The watchdog acts without a fresh
        permit, so it is restricted to what the *expired* permit already
        contracted: halt, and the deployment's contracted safe state.  It
        cannot apply a plan, cannot finalize and cannot settle -- those need a
        Kernel decision, and the point of the lease running out is that the
        Kernel may no longer be reachable.
        """
        moment = now or self._clock()
        fired: List[GatewayResult] = []
        for transaction_id in self._journal.transaction_ids():
            record = self._journal.read(transaction_id)
            if record is None or not record.watchdog_deadline:
                continue
            if record.phase in _WATCHDOG_SETTLED:
                continue
            if parse_utc(moment) < parse_utc(record.watchdog_deadline):
                continue
            expired = KernelToken.from_canonical_dict(record.last_token)
            staged = ActuationPlan.from_mapping(record.plan)
            participants = tuple(record.participants) or (record.adapter,)
            refs: Tuple[str, ...] = ()
            for position, name in enumerate(reversed(participants)):
                _, halt_refs = self._dispatch(
                    self._registry.resolve(name),
                    expired,
                    GatewayOperation.HALT,
                    scope=staged.scope,
                    index=position,
                    reference_suffix=("" if len(participants) == 1 else f":{name}"),
                )
                refs = refs + halt_refs
            result, _ = self._drive_to_safe_state(
                expired,
                record.adapter,
                record,
                staged.scope,
                staged,
                prefix_refs=refs,
                reason="watchdog: the lease expired with the change outstanding",
            )
            fired.append(result)
        return tuple(fired)

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _guard(
        self, token: KernelToken, kind: TokenKind, *, cacheable: bool = True
    ) -> Tuple[Optional[TransactionRecord], Optional[GatewayResult]]:
        """Run the permit checks that precede every operation."""
        if not isinstance(token, KernelToken):
            raise GatewayRefusal("a gateway operation takes a KernelToken and no other authority")
        if not token.authorises(kind):
            self._note_refusal(
                token, kind, f"permit authorises {token.token_kind.value}, not {kind.value}"
            )
            raise GatewayRefusal(
                f"permit authorises {token.token_kind.value}, not {kind.value}; "
                "nothing reached the equipment"
            )
        record = self._journal.read(token.transaction_id)
        if token.is_expired(self._clock()):
            return record, self._reject(
                token, record, GatewayOutcome.REJECTED_LEASE_EXPIRED, "the lease had expired"
            )
        if record is not None:
            newest = KernelToken.from_canonical_dict(record.last_token)
            if newest.fences_out(token):
                return record, self._reject(
                    token,
                    record,
                    GatewayOutcome.REJECTED_FENCE,
                    f"fence {token.fencing_token}/{token.command_sequence} is superseded by "
                    f"{newest.fencing_token}/{newest.command_sequence}",
                )
            if record.trial_id != token.trial_id:
                return record, self._reject(
                    token,
                    record,
                    GatewayOutcome.REJECTED,
                    "the permit names a different trial than the durable transaction",
                )
        effect = self._lookup_effect(token.idempotency_key)
        if effect is not None:
            if effect["tokenHash"] != token.content_hash():
                return record, self._reject(
                    token,
                    record,
                    GatewayOutcome.REJECTED_IDEMPOTENCY_COLLISION,
                    f"idempotency key already produced a {effect['kind']} effect",
                )
            if cacheable:
                return record, GatewayResult(
                    outcome=GatewayOutcome.ALREADY_APPLIED,
                    observed_config_hash=effect["observedConfigHash"],
                    evidence_refs=tuple(effect["evidenceRefs"]),
                    detail=f"retransmission of {effect['kind']}; no second effect",
                )
        return record, None

    # -- multi-participant resolution --------------------------------------

    def _axis_adapter_name(self, axis: str, default: str) -> str:
        """Which client owns *axis*; the transaction's own adapter by default."""
        return self._axis_adapters.get(axis, default)

    def _read_participants(self, default: str) -> Tuple[str, ...]:
        """Every client that must be read to see the whole surface, in order.

        The transaction's own adapter leads.  With no axis ownership declared
        this is a one-element tuple and the read path is the one it always was.
        """
        ordered = [default]
        for axis in sorted(self._surface):
            name = self._axis_adapter_name(axis, default)
            if name not in ordered:
                ordered.append(name)
        return tuple(ordered)

    def _step_adapter_name(self, staged: ActuationPlan, axis: str) -> str:
        """Which client writes *axis* for this staged plan."""
        declared = staged.adapter_for(axis)
        if declared != staged.adapter:
            return declared
        return self._axis_adapter_name(axis, staged.adapter)

    def _plan_participants(self, staged: ActuationPlan) -> Tuple[str, ...]:
        """Every client this plan writes through, in first-write order.

        The steps decide the order; the plan's own adapter is added only when no step names
        it.  Putting it first regardless made a power-first joint plan (507e23efc) halt as
        cap -> power -> steering, restoring gnb2 power while its only UE was still away
        (Codex review 2026-09-26)."""
        ordered: List[str] = []
        for step in staged.steps:
            name = self._step_adapter_name(staged, step.axis)
            if name not in ordered:
                ordered.append(name)
        if staged.adapter not in ordered:
            ordered.append(staged.adapter)
        return tuple(ordered)

    def _lookup_effect(self, idempotency_key: str) -> Optional[Mapping[str, Any]]:
        """The recorded effect of *idempotency_key*, from any transaction.

        Scanned across transactions on purpose: a key reused for a different
        transaction is still a reused key, and a per-transaction ledger would
        not see it.
        """
        for transaction_id in self._journal.transaction_ids():
            record = self._journal.read(transaction_id)
            if record is not None and idempotency_key in record.effects:
                return record.effects[idempotency_key]
        return None

    def _note_refusal(self, token: KernelToken, kind: TokenKind, detail: str) -> None:
        self._refusals.append(
            {
                "at": self._clock(),
                "transactionId": token.transaction_id,
                "trialId": token.trial_id,
                "requestedKind": kind.value,
                "permitKind": token.token_kind.value,
                "fencingToken": token.fencing_token,
                "commandSequence": token.command_sequence,
                "idempotencyKey": token.idempotency_key,
                "detail": detail,
            }
        )

    def _reject(
        self,
        token: KernelToken,
        record: Optional[TransactionRecord],
        outcome: GatewayOutcome,
        detail: str,
        refs: Tuple[str, ...] = (),
        *,
        observed: Optional[str] = None,
    ) -> GatewayResult:
        """Record a refusal and return it.  Never a silent drop."""
        self._note_refusal(token, token.token_kind, f"{outcome.value}: {detail}")
        if record is not None and observed is not None:
            self._journal.write(
                record.evolve(observed_config_hash=observed, updated_at=self._clock())
            )
        return GatewayResult(
            outcome=outcome, observed_config_hash=observed, evidence_refs=refs, detail=detail
        )

    def _settle(
        self,
        token: KernelToken,
        record: TransactionRecord,
        outcome: GatewayOutcome,
        detail: str,
        refs: Tuple[str, ...] = (),
        *,
        observed: Optional[str] = None,
    ) -> GatewayResult:
        """Durably record the result of an accepted operation and return it."""
        result = GatewayResult(
            outcome=outcome, observed_config_hash=observed, evidence_refs=refs, detail=detail
        )
        updated = record.evolve(
            last_token=token.to_canonical_dict(),
            evidence_refs=tuple(record.evidence_refs) + tuple(refs),
            updated_at=self._clock(),
        ).with_effect(
            token.idempotency_key,
            {
                "tokenHash": token.content_hash(),
                "kind": token.token_kind.value,
                "outcome": outcome.value,
                "observedConfigHash": observed,
                "evidenceRefs": list(refs),
                "detail": detail,
            },
        )
        self._journal.write(updated)
        return result

    def _dispatch(
        self,
        adapter: WriteGatewayAdapter,
        token: KernelToken,
        operation: GatewayOperation,
        *,
        scope: Mapping[str, Any],
        axis: Optional[str] = None,
        value: Any = None,
        index: int = 0,
        watchdog_id: Optional[str] = None,
        reference_suffix: str = "",
    ) -> Tuple[GatewayResult, Tuple[str, ...]]:
        """Send one already-validated command and file what came back.

        ``reference_suffix`` distinguishes the same operation asked of two
        participants at the same step index.  It is empty for every
        single-participant transaction, so the evidence references a
        one-adapter deployment produces do not move.
        """
        command = build_command(
            token,
            operation,
            scope=scope,
            axis=axis,
            value=value,
            index=index,
            watchdog_id=watchdog_id,
        )
        try:
            result = adapter.dispatch(token=token, command=command)
        except Exception as exc:  # an adapter that raises must not lose the record
            result = GatewayResult(
                outcome=(
                    GatewayOutcome.ERROR
                    if operation in (GatewayOperation.READ, GatewayOperation.VALIDATE)
                    else GatewayOutcome.UNKNOWN
                ),
                # Keep the message: the class alone ("adapter raised R1Error")
                # cannot tell a refused policy type from a timeout (2026-09-23 audit).
                detail=f"adapter raised {type(exc).__name__}: {exc}"[:300],
            )
        if not isinstance(result, GatewayResult):
            result = GatewayResult(
                outcome=GatewayOutcome.ERROR, detail="adapter did not return a GatewayResult"
            )
        reference = (
            f"{token.transaction_id}#{token.command_sequence}:{operation.value}:{index}"
            + reference_suffix
        )
        self._evidence[reference] = {
            "at": self._clock(),
            "operation": operation.value,
            "command": dict(command),
            "commandHash": command_hash(command),
            "outcome": result.outcome.value,
            "observedConfigHash": result.observed_config_hash,
            "adapterEvidenceRefs": list(result.evidence_refs),
            "detail": result.detail,
        }
        if result.observed_config is not None:
            self._evidence[reference]["observedConfig"] = dict(result.observed_config)
        return result, (reference,) + tuple(result.evidence_refs)

    def _wait_for_the_clock(self, seconds: float) -> None:
        """Wait only on a clock that moves: an injected frozen clock (hermetic tests) would
        read the same instant after any real wait, so the retry reads at once."""
        first = self._clock()
        self._sleep(0.002)
        if self._clock() != first:
            self._sleep(seconds)

    def _observe(
        self,
        participants: Tuple[str, ...],
        token: KernelToken,
        scope: Mapping[str, Any],
        index: int = 0,
        plan: Optional[ActuationPlan] = None,
    ) -> Tuple[Optional[str], Tuple[str, ...]]:
        """Read the live configuration digest, or ``None`` when it cannot be read.

        One participant answers with the digest of the whole surface, which is
        what a single-client deployment has always done.  Two or more can each
        see only their own axes, so each must *name* what it observed and the
        gateway merges the parts before hashing: a digest of half the surface
        would compare equal to nothing and turn every read into ``UNKNOWN``.
        A participant that cannot name its axes in a composition is an
        unreadable configuration, not a partial one.
        """
        refs: Tuple[str, ...] = ()
        self._unread_reason = ""
        if len(participants) == 1:
            adapter = self._registry.resolve(participants[0])
            result, refs = self._dispatch(
                adapter, token, GatewayOperation.READ, scope=scope, index=index
            )
            if result.outcome is GatewayOutcome.ACKED and result.observed_config_hash:
                return result.observed_config_hash, refs
            self._unread_reason = (
                f"{participants[0]} answered {result.outcome.value}"
                + (f": {result.detail}" if result.detail else "")
            )
            return None, refs
        merged: Dict[str, Any] = {}
        untouched = (self._untouched_participants(participants, plan)
                     if plan is not None else {})
        self._excluded_participants = []
        for position, name in enumerate(participants):
            adapter = self._registry.resolve(name)
            result, step_refs = self._dispatch(
                adapter,
                token,
                GatewayOperation.READ,
                scope=scope,
                index=index,
                reference_suffix=f":{name}",
            )
            refs = refs + step_refs
            if (result.outcome is not GatewayOutcome.ACKED or result.observed_config is None) \
                    and name in untouched:
                # 2026-09-23 오너 지시: "무관한 ue가 떨어져도 그냥 떨어진 부분은 제외하고 다시
                # 붙여서 진행해".  이 거래가 **건드리지 않는** 축만 가진 참여자가 읽히지 않으면
                # (그 UE 가 떨어졌다 -- 시도 452: ue1 cap 을 되돌린 뒤 ue2 가 이탈해 공동 읽기가
                # 통째로 UNKNOWN, 판이 RECOVERY_FAILURE), 그 참여자는 빼고 계획의 기준값으로
                # 채운다.  **관측한 척하지 않는다**: 제외 사실이 증거 참조에 남는다.  이 거래가
                # 쓴 축의 참여자는 여전히 읽혀야 하고, 비상 안전상태 읽기는 계획을 넘기지 않으므로
                # 예외가 없다.
                for axis, value in untouched[name].items():
                    merged.setdefault(axis, value)
                refs = refs + (f"{name}:excluded:unread-untouched-by-this-transaction",)
                self._excluded_participants.append(name)
                continue
            if result.outcome is not GatewayOutcome.ACKED or result.observed_config is None:
                self._unread_reason = (
                    f"{name} answered {result.outcome.value}"
                    + (f": {result.detail}" if result.detail else "")
                    + (" and named no axes" if result.observed_config is None
                       and result.outcome is GatewayOutcome.ACKED else "")
                )
                return None, refs
            for axis, value in dict(result.observed_config).items():
                if axis in merged and merged[axis] != value:
                    # Two clients disagree about one axis.  Nobody may pick.
                    self._unread_reason = (
                        f"{name} reads {axis}={value!r} where an earlier "
                        f"participant read {merged[axis]!r}"
                    )
                    return None, refs
                merged[axis] = value
            del position
        if self._excluded_participants and len(self._excluded_participants) == len(participants):
            # 모두 제외됐다면 관측한 것이 하나도 없다 -- 기준선이라고 말할 근거가 없다.
            self._unread_reason = ("every participant was unreadable: "
                                   + ", ".join(self._excluded_participants))
            return None, refs
        if set(merged) != self._surface:
            self._unread_reason = (
                "the participants between them named "
                f"{sorted(set(merged))}, which is not the surface "
                f"{sorted(self._surface)}"
            )
            return None, refs
        return config_hash(merged), refs

    def _untouched_participants(
        self, participants: Tuple[str, ...], plan: ActuationPlan
    ) -> Dict[str, Dict[str, Any]]:
        """Participant -> {axis: baseline} for every participant whose axes this plan
        leaves at their baseline.  Only these may be excluded when unreadable."""
        planned = {step.axis: step.value for step in plan.steps}
        baseline = dict(plan.baseline_config)
        default = participants[0] if participants else ""
        found: Dict[str, Dict[str, Any]] = {}
        # 2026-09-23 (시도 456 시행 3·4): 첫 참여자를 "거래의 자기 어댑터" 라며 늘 읽게 했는데,
        # 공동 계획에서 그건 **순서상 첫째**일 뿐이다 -- 우선순위만 바꾼 시행에서 건드리지 않은
        # `r1-steer@ue1` 이 못 읽혀 두 번 잠겼다.  기준은 "이 거래가 건드렸는가" 하나다.
        # 아무것도 못 읽은 읽기는 `_observe` 가 따로 거절한다.
        for name in participants:
            owned = [axis for axis in self._surface
                     if self._axis_adapter_name(axis, default) == name]
            if not owned or any(axis not in baseline for axis in owned):
                continue
            if all(str(planned.get(axis, baseline[axis])) == str(baseline[axis])
                   for axis in owned):
                found[name] = {axis: baseline[axis] for axis in owned}
        return found

    #: Participants the last :meth:`_observe` excluded as unread and untouched.
    _excluded_participants: List[str] = []

    #: Why the last :meth:`_observe` could not read, for the refusal that reports it.
    #: 2026-09-16 attempt 130 ended EXECUTION_FAILURE on two ``PREPARE`` reads that said
    #: only "the configuration could not be read"; the participant that failed and its own
    #: detail were both discarded, so the episode could not be diagnosed from its evidence.
    _unread_reason: str = ""

    def _why_unread(self) -> str:
        """The reason clause to append to an unreadable-configuration refusal."""
        return f" ({self._unread_reason})" if self._unread_reason else ""

    @staticmethod
    def _largest_prefix(prefixes: Tuple[str, ...], observed: str) -> Optional[int]:
        """The most-applied prefix the observation is consistent with.

        Largest rather than first: a step that writes an axis its current value
        makes two prefixes share a digest, and assuming *more* was applied
        makes the reversal cover more, which is the safe direction to be wrong
        in.
        """
        found = None
        for index, digest in enumerate(prefixes):
            if digest == observed:
                found = index
        return found

    def _reconcile_apply(
        self,
        token: KernelToken,
        record: TransactionRecord,
        staged: ActuationPlan,
        acked: int,
        lost_ack: bool,
        failure: str,
        refs: Tuple[str, ...],
        *,
        moved_acked: int = 0,
    ) -> GatewayResult:
        """Decide what an apply actually did, from the readback and not the acks.

        Design section 9: an A1 create or an E2 acknowledgement is not success.
        The configuration reread is what decides, and where it cannot decide
        the answer is ``UNKNOWN`` -- never ``REJECTED``, which would tell the
        Kernel nothing happened when something may have.
        """
        total = len(staged.steps)
        observed, confirm_refs = self._observe(
            self._read_participants(record.adapter), token, staged.scope, index=total + 1,
            plan=staged,
        )
        refs = refs + confirm_refs
        prefixes = staged.prefix_hashes()
        if observed is None:
            return self._settle(
                token,
                record.evolve(
                    phase=TransactionPhase.UNCERTAIN,
                    next_safe_action=NextSafeAction.RECOVERY_CONFIRM,
                    applied_axes=staged.axes,
                    watchdog_deadline=token.lease_expiry,
                ),
                GatewayOutcome.UNKNOWN,
                (f"apply dispatched, readback unavailable{self._why_unread()}; "
                 f"{acked}/{total} acknowledged; {failure}").strip("; ")[:400],
                refs,
            )
        matched = self._largest_prefix(prefixes, observed)
        if matched == total:
            return self._settle(
                token,
                record.evolve(
                    phase=TransactionPhase.APPLIED,
                    next_safe_action=NextSafeAction.FINALIZE_LIVE,
                    applied_axes=staged.axes,
                    observed_config_hash=observed,
                    watchdog_deadline=token.lease_expiry,
                ),
                GatewayOutcome.ACKED,
                "readback confirms the applied configuration"
                + (" (an acknowledgement was lost)" if lost_ack else ""),
                refs,
                observed=observed,
            )
        # 2026-09-23 (시도 455 시행 1): "refused downstream" 는 **아무 움직이는 쓰기도
        # 접수되지 않았을 때만** 참이다.  접수된 쓰기(정책 생성)가 있는데 되읽기가 아직
        # 기준선이면 그건 거절이 아니라 "효과 미관측" 이고 철회 의무가 있다 -- 아래
        # PARTIAL_APPLY 분기로 간다.  예전엔 움직이는 축이 계획의 **첫** 축이면 matched 가
        # 0 이 되어 여기로 와 `applied_axes=()` 로 의무가 사라졌고, 같은 상황에서 둘째 축이면
        # PARTIAL_APPLY 였다 -- 판정이 축 순서에 달려 있었다.
        if matched == 0 and not lost_ack and not moved_acked:
            return self._settle(
                token,
                record.evolve(
                    phase=TransactionPhase.REFUSED,
                    next_safe_action=NextSafeAction.SETTLE,
                    applied_axes=(),
                    observed_config_hash=observed,
                    watchdog_deadline=None,
                ),
                GatewayOutcome.REJECTED,
                # 400, like the PARTIAL_APPLY branch below.  2026-09-17: that one
                # was widened and these two were not, so a steering failure came
                # back as "R1Error: POST <url>" -- cut one character after the
                # URL, with the transport reason gone.  The reader could not tell
                # a timeout from a refused policy type.  **All three branches of
                # this function compose the same `failure` string; widening one
                # buys nothing.**
                f"refused downstream with the baseline confirmed live; {failure}"[:400],
                refs,
                observed=observed,
            )
        if matched == 0 and not moved_acked:
            return self._settle(
                token,
                record.evolve(
                    phase=TransactionPhase.UNCERTAIN,
                    next_safe_action=NextSafeAction.RECOVERY_CONFIRM,
                    # The lost step sits at index ``acked`` (the loop breaks on
                    # it) and may yet land, so it is owed a reversal too.
                    applied_axes=staged.axes[: acked + 1],
                    observed_config_hash=observed,
                    watchdog_deadline=token.lease_expiry,
                ),
                GatewayOutcome.UNKNOWN,
                ("an acknowledgement was lost and the baseline is still live; "
                 f"the write may yet land; {failure}").strip("; ")[:400],
                refs,
                observed=observed,
            )
        # 2026-09-23 audit: a step whose answer was lost (index ``acked``) may
        # have landed; leaving it out of ``applied_axes`` meant the rollback never
        # reversed a moving axis nobody saw.  Unknown is owed a reversal.
        applied = max(matched if matched is not None else 0,
                      min(total, acked + (1 if lost_ack else 0)))
        return self._settle(
            token,
            record.evolve(
                phase=TransactionPhase.PARTIALLY_APPLIED,
                next_safe_action=NextSafeAction.REVERSE_ROLLBACK,
                applied_axes=staged.axes[:applied],
                observed_config_hash=observed,
                watchdog_deadline=token.lease_expiry,
            ),
            GatewayOutcome.PARTIAL_APPLY,
            # 400, not 200: the axis name and the adapter's own prefix eat the
            # first ~120 characters, so a producer 409 arrived cut at
            # "{'error': 'policy u" and every PARTIAL_APPLY trial of
            # 2026-09-16 was undiagnosable from the record alone.
            # ``applied`` is ``max(matched, acked)`` -- the readback's count when
            # it is ahead, otherwise the adapters' acknowledgements.  Saying
            # "are live" for the second case claims more than we know: an ACK is
            # not an observation.  So the sentence names its own basis.  (The
            # reversal detail below had the same ambiguity and it cost an hour
            # and nearly a weakened safety check on 2026-09-17.)
            # 2026-09-23: 되읽기가 **기준선 그대로**인데 여기 온 경우를 따로 말한다.
            # 계획의 첫 축이 제자리면 `prefix(1) == prefix(0)` 이라 `matched` 가 1 로
            # 나오고, 문장은 "readback confirmed only 1" 이 된다 -- 실제로는 아무 축도
            # 무선에 안 보였는데.  라우팅(되돌림 의무)은 그대로 두고 말만 바로잡는다:
            # 쓰기는 접수됐고 되돌림을 빚지고 있으며, 효과는 아직 관측되지 않았다.
            (f"{applied}/{total} axes were acknowledged but the readback still shows "
             f"the baseline: no effect observed yet, and the acknowledged writes are "
             f"owed a reversal; {failure}"
             if observed == staged.baseline_hash else
             f"{applied}/{total} axes {_applied_basis(matched, acked)}; {failure}")[:400],
            refs,
            observed=observed,
        )

    def _emergency_target(
        self, record: Optional[TransactionRecord]
    ) -> Tuple[str, Mapping[str, Any], Optional[ActuationPlan]]:
        """Pick the adapter and scope an emergency safe state acts through.

        Works with no transaction at all -- design section 11 requires it from
        any state, including one where prepare never completed.  With no record
        and more than one registered adapter there is no non-arbitrary choice,
        and an arbitrary one would write to equipment nobody named.
        """
        if record is not None:
            staged = ActuationPlan.from_mapping(record.plan) if record.plan else None
            scope = staged.scope if staged is not None else {}
            return record.adapter, scope, staged
        names = list(self._registry.registered_paths())
        if len(names) != 1:
            raise GatewayRefusal(
                "emergency safe state needs a transaction record or exactly one adapter"
            )
        return names[0], {}, None

    def _drive_to_safe_state(
        self,
        token: KernelToken,
        adapter_name: str,
        record: Optional[TransactionRecord],
        scope: Mapping[str, Any],
        staged: Optional[ActuationPlan],
        *,
        prefix_refs: Tuple[str, ...] = (),
        reason: str = "emergency safe state",
    ) -> Tuple[GatewayResult, Tuple[str, ...]]:
        """Reverse what is live, write the contracted safe state, confirm it.

        Every write goes to the client that owns the axis, so a composition's
        safe state is written on the same participants that moved it -- a
        deployment does not become safe by telling one client about an axis it
        does not serve.
        """
        refs = tuple(prefix_refs)
        index = 0
        if record is not None and staged is not None:
            for axis in reversed(record.applied_axes):
                result, step_refs = self._dispatch(
                    self._registry.resolve(self._step_adapter_name(staged, axis)),
                    token,
                    GatewayOperation.UNDO,
                    scope=scope,
                    axis=axis,
                    value=staged.baseline_config[axis],
                    index=index,
                )
                refs = refs + step_refs
                index += 1
        for axis, value in sorted(self._safe_state.items()):
            result, step_refs = self._dispatch(
                self._registry.resolve(self._axis_adapter_name(axis, adapter_name)),
                token,
                GatewayOperation.APPLY,
                scope=scope,
                axis=axis,
                value=value,
                index=index,
            )
            refs = refs + step_refs
            index += 1
        observed, confirm_refs = self._observe(
            self._read_participants(adapter_name), token, scope, index=index
        )
        refs = refs + confirm_refs
        if observed is None:
            outcome, detail = GatewayOutcome.UNKNOWN, f"{reason}: safe state could not be confirmed"
        elif observed == self._safe_state_hash:
            outcome, detail = GatewayOutcome.ACKED, f"{reason}: contracted safe state confirmed"
        else:
            outcome, detail = (
                GatewayOutcome.PARTIAL_APPLY,
                f"{reason}: the deployment did not reach the contracted safe state",
            )
        base = record if record is not None else TransactionRecord(
            transaction_id=token.transaction_id,
            trial_id=token.trial_id,
            phase=TransactionPhase.SAFE_STATE,
            adapter=adapter_name,
            baseline_config_hash=self._safe_state_hash,
            applied_config_hash=self._safe_state_hash,
            plan={},
            last_token=token.to_canonical_dict(),
            updated_at=self._clock(),
        )
        updated = base.evolve(
            phase=(
                TransactionPhase.SAFE_STATE
                if outcome is GatewayOutcome.ACKED
                else TransactionPhase.UNCERTAIN
            ),
            next_safe_action=(
                NextSafeAction.SETTLE
                if outcome is GatewayOutcome.ACKED
                else NextSafeAction.RECOVERY_CONFIRM
            ),
            applied_axes=(),
            observed_config_hash=observed,
            watchdog_deadline=None,
        )
        return (
            self._settle(token, updated, outcome, detail, refs, observed=observed),
            refs,
        )
