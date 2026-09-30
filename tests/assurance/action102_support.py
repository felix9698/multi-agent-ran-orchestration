"""Shared hermetic fixtures for the SUPPLEMENTARY UE DL PRB cap tests.

Not a test module.  It stands the in-repo Campaign 5 producer up behind an
``R1PolicyPort``-shaped bridge and stands in for the near-RT worker, so the
seven-case fault matrix can be driven without a radio.

**Where a write count comes from.**  The fault matrix is mostly assertions
about how many writes happened -- "exactly one", "zero", "one restore" -- and
those numbers are read off *production* records, never off this file's
bookkeeping:

``R1Adapter.write_counts()``
    The adapter's append-only operation journal
    (``assurance/gateway/r1_operation_journal.py``): every policy-port call it
    issued, with the outcome the port gave.  This is what the gateway caused.
``Campaign5PolicyProducer.control_records()``
    The producer's own history of every control outcome the worker reported,
    with ``writeMayHaveOccurred`` and the episode state.  This is what reached
    the RAN, as far as anything in the repository can say.
``InMemoryTransactionJournal.read()``
    The gateway's transaction record: which axes were applied, by which
    participants, and whether the transaction is uncertain.

:func:`CapHarness.control_evidence` combines the first two into the numbers a
test asserts.  ``CountingCapPolicyPort.e2_writes`` survives as a **diagnostic**
-- useful when a failure needs explaining -- and
:meth:`CapHarness.assert_diagnostic_agrees` proves it never diverges from the
production records rather than letting it quietly become the source of truth.

Everything here is hermetic: no socket, no radio, no subprocess, no model.  The
Kernel, the Write Gateway, the ``R1Adapter``, the durable binding journal, the
producer and the corroborated readback are all the real ones; the near-RT
worker, the gNB scheduler and the KPM stream are the parts stood in for.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from assurance.gateway.gateway import TokenBoundWriteGateway
from assurance.gateway.journal import InMemoryTransactionJournal
from assurance.gateway.live import scalar_leaf_readback
from assurance.gateway.mock_adapter import MockActuationAdapter
from assurance.gateway.r1_adapter import R1Adapter
from assurance.gateway.r1_binding_journal import InMemoryR1BindingJournal
from assurance.gateway.r1_operation_journal import InMemoryR1OperationJournal
from assurance.gateway.token import KernelToken, TokenKind
from assurance.objectives.action102_support import (
    CAP_ACTION_ID, SUPPLEMENTARY_ACTIONS,
)

from oran.campaign5.builders import make_policy_builder
from oran.campaign5.families import CAMPAIGN5_FAMILIES
from oran.campaign5.builders import Campaign5BuilderError
from oran.campaign5.producer import A1Conflict, A1Error, Campaign5PolicyProducer
from oran.campaign5.readback import (
    CorroboratedConfigReadback, DictKpmConfigReader, make_status_projection,
)

CLOCK = "2026-09-04T09:00:00.000000Z"
LEASE = "2026-09-04T10:00:00.000000Z"
#: A permit issued before the gateway's clock whose lease has already run out
#: by the time a command arrives.
EARLIER = "2026-09-04T08:00:00.000000Z"
EXPIRED_LEASE = "2026-09-04T08:30:00.000000Z"
VALIDITY = {"notBefore": "2026-09-04T00:00:00Z", "notAfter": "2026-09-05T00:00:00Z"}

CAP = SUPPLEMENTARY_ACTIONS[CAP_ACTION_ID]
CAP_FAMILY = CAMPAIGN5_FAMILIES["cap"]

#: The objective UE the target protects, and the heavy non-target UE a cap may
#: control.  Two identities, never one: the whole safety argument is that they
#: are different.
OBJECTIVE_UE = {"cellId": "12345678", "ueId": "131"}
CONTROLLED_UE = {"cellId": "12345678", "ueId": "132"}

STEERING_AXIS = "servingCell"
HOME_NCI = "12345678"
TARGET_NCI = "87654321"
UNCAPPED = CAP.baseline
APPLIED_CAP = "12"


def plan_scope() -> Dict[str, Any]:
    """The plan scope: the objective UE, with the controlled UE in its own key."""
    return {**OBJECTIVE_UE, "controlledUe": dict(CONTROLLED_UE),
            "controlledUeId": CONTROLLED_UE["ueId"]}


def baseline_config() -> Dict[str, str]:
    return {STEERING_AXIS: HOME_NCI, CAP.axis: UNCAPPED}


def applied_config() -> Dict[str, str]:
    return {STEERING_AXIS: TARGET_NCI, CAP.axis: APPLIED_CAP}


def permit(kind: str, expected: str, sequence: int, *, transaction_id: str = "tx-cap",
           trial_id: str = "trial-cap", fence: int = 2,
           lease: str = LEASE, key: Optional[str] = None,
           issued_at: str = CLOCK) -> KernelToken:
    return KernelToken(
        token_kind=TokenKind[kind], transaction_id=transaction_id, trial_id=trial_id,
        fencing_token=fence, command_sequence=sequence, lease_expiry=lease,
        expected_config_hash=expected,
        idempotency_key=key or f"{transaction_id}:{kind}:{fence}:{sequence}",
        issued_at=issued_at,
    )


@dataclass
class CapFaults:
    """Which failures the stood-in near-RT worker and gNB should produce."""

    #: The producer refuses before accepting a policy (HTTP 400/409 class).
    refuse_create: Optional[type] = None
    #: The UE attribution is stale/absent: the worker performs zero E2 writes.
    stale_attribution: bool = False
    #: The E2 write lands and the acknowledgement does not.
    drop_control_ack: bool = False
    #: The configuration counter is not in the stream after the write.
    suppress_counter: bool = False
    #: The scheduler applies a different value than the one requested.
    applied_offset: int = 0
    #: DELETE is accepted but the restore never reaches the scheduler.
    suppress_restore: bool = False


class CountingCapPolicyPort:
    """``R1PolicyPort`` over the in-repo producer, standing in for the worker.

    One CONTROL per accepted policy revision and one per restore, exactly as
    the released worker sends them.  A replay of an identical body under the
    same idempotency key produces **no** second write: the producer answers with
    the prior response, so this bridge never reaches the scheduler again.

    It keeps :attr:`e2_writes` as a **diagnostic** list.  No assertion about
    safety is made against it: the numbers come from the adapter's operation
    journal and the producer's control records, and
    :meth:`CapHarness.assert_diagnostic_agrees` checks this list against them.
    """

    def __init__(self, producer: Campaign5PolicyProducer, kpm: DictKpmConfigReader,
                 *, faults: Optional[CapFaults] = None) -> None:
        self.producer = producer
        self.kpm = kpm
        self.faults = faults or CapFaults()
        self._ids = (f"pol-cap-{n}" for n in itertools.count(1))
        self._type_of: Dict[str, str] = {}
        self._bodies: Dict[str, str] = {}
        self._baseline: Dict[str, int] = {}
        #: Every E2 CONTROL this bridge caused, in order.  Diagnostic only:
        #: fixture-owned bookkeeping, cross-checked against the production
        #: records rather than asserted on.
        self.e2_writes: List[Dict[str, Any]] = []
        #: Every transport call, for the evidence record.
        self.calls: List[Tuple[str, str]] = []
        self.deleted: List[str] = []

    # -- the port surface -------------------------------------------------

    def get_policy_type(self, policy_type_id: str) -> Mapping[str, Any]:
        self.calls.append(("get_policy_type", policy_type_id))
        return self.producer.get_policytype(policy_type_id)

    def create_policy(self, ric: str, policy_type_id: str,
                      body: Mapping[str, Any]) -> Mapping[str, Any]:
        del ric
        self.calls.append(("create_policy", policy_type_id))
        if self.faults.refuse_create is not None:
            raise self.faults.refuse_create("injected producer refusal")
        digest = _digest(body)
        for policy_id, seen in self._bodies.items():
            if seen == digest:
                # A replay: the producer returns the prior response and the
                # worker sends nothing.  Exactly one write still stands.
                return {"policyId": policy_id}
        policy_id = next(self._ids)
        self._type_of[policy_id] = policy_type_id
        self.producer.put_policy(policy_type_id, policy_id, body)
        self._bodies[policy_id] = digest
        self._control(policy_id, body)
        return {"policyId": policy_id}

    def update_policy(self, policy_id: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append(("update_policy", policy_id))
        digest = _digest(body)
        if self._bodies.get(policy_id) == digest:
            return {"policyId": policy_id}
        self.producer.put_policy(self._type_of[policy_id], policy_id, body)
        self._bodies[policy_id] = digest
        self._control(policy_id, body)
        return {"policyId": policy_id}

    def delete_policy(self, policy_id: str) -> None:
        self.calls.append(("delete_policy", policy_id))
        self.deleted.append(policy_id)
        baseline = self._baseline.get(policy_id)
        if self.faults.suppress_restore or baseline is None:
            return
        scope = {name: self.producer.get_policy(self._type_of[policy_id],
                                                policy_id)["config"][name]
                 for name in CAP_FAMILY.scope_fields}
        self.e2_writes.append({"kind": "RESTORE", "policyId": policy_id,
                               "value": baseline})
        self.kpm.publish(CAP_FAMILY.readback_counter, scope, {"maxDlPrbs": baseline})
        self.producer.record_applied(
            self._type_of[policy_id], policy_id, control_ack=True,
            observed_config={**scope, "maxDlPrbs": baseline})

    def get_policy_status(self, policy_id: str) -> Mapping[str, Any]:
        self.calls.append(("get_policy_status", policy_id))
        return self.producer.get_status(self._type_of[policy_id], policy_id)

    # -- the stood-in worker ----------------------------------------------

    def _control(self, policy_id: str, body: Mapping[str, Any]) -> None:
        config = dict(body["config"])
        scope = {name: config[name] for name in CAP_FAMILY.scope_fields}
        if self.faults.stale_attribution:
            # No fresh attribution: REFUSED_STALE_TARGET, zero E2 writes, and
            # no fallback to a cached RNTI.
            self.producer.record_applied(
                self._type_of[policy_id], policy_id, control_ack=False)
            return
        self._baseline.setdefault(policy_id, _current(self.kpm, scope))
        applied = int(config["maxDlPrbs"]) + int(self.faults.applied_offset)
        self.e2_writes.append({"kind": "APPLY", "policyId": policy_id,
                               "value": applied})
        if self.faults.drop_control_ack:
            # The write landed; the acknowledgement did not.  The producer must
            # not call that verified.
            self.producer.record_applied(
                self._type_of[policy_id], policy_id, control_ack=True,
                observed_config=None)
            return
        if self.faults.suppress_counter:
            self.producer.record_applied(
                self._type_of[policy_id], policy_id, control_ack=True,
                observed_config=None)
            return
        self.kpm.publish(CAP_FAMILY.readback_counter, scope, {"maxDlPrbs": applied})
        self.producer.record_applied(
            self._type_of[policy_id], policy_id, control_ack=True,
            observed_config={**scope, "maxDlPrbs": applied})

    # -- diagnostics -------------------------------------------------------
    #
    # Fixture-owned counts.  Kept for failure messages and cross-checked by
    # ``CapHarness.assert_diagnostic_agrees``; never the basis of an assertion
    # about what reached the equipment.

    @property
    def diagnostic_write_count(self) -> int:
        return len(self.e2_writes)

    @property
    def diagnostic_apply_count(self) -> int:
        return len([write for write in self.e2_writes if write["kind"] == "APPLY"])

    @property
    def diagnostic_restore_count(self) -> int:
        return len([write for write in self.e2_writes if write["kind"] == "RESTORE"])


class ControlledScopeCounterReader:
    """Read the configuration counter at the **controlled** UE's scope.

    The gateway hands a readback the *plan* scope, which names the objective UE
    because the objective is about that UE.  A supplementary control acts on a
    different one and the plan carries it in its own key, so this is where the
    two are swapped -- once, explicitly, and refusing rather than falling back:
    a counter read at the objective UE's scope would report the wrong UE's
    configuration and call the cap verified on it.
    """

    def __init__(self, kpm: DictKpmConfigReader) -> None:
        self._kpm = kpm

    def read(self, counter_name: str, scope: Mapping[str, Any]
             ) -> Optional[Mapping[str, Any]]:
        controlled = scope.get("controlledUe")
        if not isinstance(controlled, Mapping) or not controlled:
            return None
        return self._kpm.read(counter_name, dict(controlled))


def _digest(body: Mapping[str, Any]) -> str:
    from oran.contract.jcs import jcs_sha256

    return jcs_sha256(dict(body))


def _current(kpm: DictKpmConfigReader, scope: Mapping[str, Any]) -> int:
    observed = kpm.read(CAP_FAMILY.readback_counter, scope)
    return int(observed["maxDlPrbs"]) if observed else int(UNCAPPED)


class _StepClock:
    """A deterministic clock; nothing hermetic waits on wall time."""

    def __init__(self) -> None:
        self.ms = 0

    def now(self) -> str:
        return CLOCK

    def monotonic_ms(self) -> int:
        return self.ms

    def sleep_ms(self, milliseconds: int) -> None:
        self.ms += max(0, int(milliseconds))


@dataclass(frozen=True)
class ControlEvidence:
    """Write accounting read off the production records, never off a fixture.

    Two independent sources, kept separate because they answer different
    questions and a fault is often the gap between them:

    *the adapter's operation journal* -- what the gateway **caused**.
        ``applies_sent`` / ``withdrawals_sent`` count the policy-port calls
        the port accepted; ``refused`` counts the ones a producer answered *no*
        to, which reached nothing; ``unknown`` counts the ones whose fate the
        adapter cannot state and a recovery must treat as possible writes.
    *the producer's control records* -- what **reached the RAN**, as far as
        anything in the repository can say.  ``controls_reaching_ran`` counts
        the reported controls with ``writeMayHaveOccurred``;
        ``controls_failed`` the ones that NACKed before the scheduler saw them;
        ``verified`` the ones a corroborated readback confirmed.

    A stale RNTI is exactly ``applies_sent == 1`` with
    ``controls_reaching_ran == 0``: the policy was created and no write left the
    xApp.  Collapsing the two numbers into one would lose that.
    """

    applies_sent: int
    withdrawals_sent: int
    refused: int
    unknown: int
    controls_reaching_ran: int
    controls_failed: int
    verified: int

    @property
    def writes_sent(self) -> int:
        """Every write call that may have reached the equipment."""
        return self.applies_sent + self.withdrawals_sent


@dataclass
class CapHarness:
    """One composed cap participant plus the gateway it is registered in."""

    gateway: TokenBoundWriteGateway
    adapter: R1Adapter
    steering: MockActuationAdapter
    port: CountingCapPolicyPort
    producer: Campaign5PolicyProducer
    kpm: DictKpmConfigReader
    binding_journal: InMemoryR1BindingJournal
    operation_journal: InMemoryR1OperationJournal
    transaction_journal: InMemoryTransactionJournal
    clock: _StepClock
    faults: CapFaults = field(default_factory=CapFaults)

    def plan(self, *, steering: bool = True) -> Dict[str, Any]:
        steps = []
        if steering:
            steps.append({"axis": STEERING_AXIS, "value": TARGET_NCI})
        steps.append({"axis": CAP.axis, "value": APPLIED_CAP, "adapter": CAP.adapter})
        return {
            "adapter": "mock", "scope": plan_scope(),
            "baselineConfig": baseline_config(), "steps": steps, "watchdogs": [],
        }

    def cap_binding(self, transaction_id: str = "tx-cap") -> Any:
        return self.binding_journal.binding_for(transaction_id)

    def live_cap(self) -> int:
        return _current(self.kpm, CONTROLLED_UE)

    # -- what the production records say -----------------------------------

    def control_evidence(self, transaction_id: str = "tx-cap") -> ControlEvidence:
        """Write accounting for *transaction_id*, from the production records.

        The adapter's operation journal is scoped to the transaction; the
        producer's control records are scoped to the cap policy type, which is
        the same set here because one harness composes one cap participant.
        """
        counts = self.adapter.write_counts(transaction_id)
        controls = self.producer.control_records(CAP_FAMILY.policy_type_id)
        return ControlEvidence(
            applies_sent=counts["applies"],
            withdrawals_sent=counts["withdrawals"],
            refused=counts["refused"],
            unknown=counts["unknown"],
            controls_reaching_ran=len(
                [entry for entry in controls if entry["writeMayHaveOccurred"]]),
            controls_failed=len(
                [entry for entry in controls if not entry["writeMayHaveOccurred"]]),
            verified=len([entry for entry in controls
                          if entry["episodeState"] == "APPLIED_VERIFIED"]),
        )

    def assert_diagnostic_agrees(self, case: Any,
                                 transaction_id: str = "tx-cap") -> ControlEvidence:
        """Check the fixture's diagnostic list against the production records.

        The stood-in worker's ``e2_writes`` list is the only place that knows
        what the *scheduler* saw, and it is fixture-owned, so it is checked
        rather than trusted: one diagnostic entry per control the producer
        recorded as having reached the RAN, and never more applies than the
        adapter issued.  A divergence is a fixture bug and fails here instead
        of silently becoming the number a safety claim rests on.
        """
        evidence = self.control_evidence(transaction_id)
        case.assertEqual(
            self.port.diagnostic_write_count, evidence.controls_reaching_ran,
            "the diagnostic write list disagrees with the producer's control records")
        case.assertLessEqual(
            self.port.diagnostic_apply_count, evidence.applies_sent,
            "the diagnostic counted more applies than the adapter issued")
        case.assertLessEqual(
            self.port.diagnostic_restore_count, evidence.withdrawals_sent,
            "the diagnostic counted more restores than the adapter withdrew")
        return evidence


def build_cap_harness(faults: Optional[CapFaults] = None,
                      *, kpm: Optional[DictKpmConfigReader] = None,
                      binding_journal: Optional[InMemoryR1BindingJournal] = None,
                      operation_journal: Optional[Any] = None,
                      transaction_journal: Optional[InMemoryTransactionJournal] = None,
                      policy_builder: Optional[Any] = None,
                      port: Optional[CountingCapPolicyPort] = None,
                      port_class: type = None,
                      ) -> CapHarness:
    """Compose the real gateway over a mock steering adapter and a real ``r1-cap``.

    ``binding_journal`` / ``operation_journal`` / ``policy_builder`` are the
    seams a restart test needs: hand a fresh process the durable journals a
    crashed one left and rebuild everything else, which is how the revision
    contract is proved rather than asserted.  ``port`` carries the *producer*
    across such a restart -- the A1-P side does not forget its policies because
    the rApp died, and a restart test that let it would be testing nothing.
    """
    clock = _StepClock()
    if port is not None:
        stream = port.kpm
        producer = port.producer
    else:
        stream = kpm if kpm is not None else DictKpmConfigReader()
        stream.publish(CAP_FAMILY.readback_counter, CONTROLLED_UE,
                       {"maxDlPrbs": int(UNCAPPED)})
        producer = Campaign5PolicyProducer()
        port = (port_class or CountingCapPolicyPort)(
            producer, stream, faults=faults)
    bindings = (binding_journal if binding_journal is not None
                else InMemoryR1BindingJournal())
    readback = CorroboratedConfigReadback(
        CAP_FAMILY, status_port=port, kpm_reader=ControlledScopeCounterReader(stream),
        monotonic_ms=clock.monotonic_ms, sleep_ms=clock.sleep_ms,
        cadence_ms=100, deadline_ms=1000,
    )
    operations = (operation_journal if operation_journal is not None
                  else InMemoryR1OperationJournal())
    cap_adapter = R1Adapter(
        policy_port=port,
        policy_builder=(policy_builder if policy_builder is not None
                        else controlled_scope_builder()),
        operation_journal=operations,
        near_rt_ric_id="near-rt-ric-hermetic",
        policy_type_id=CAP_FAMILY.policy_type_id,
        readback_port=scalar_leaf_readback(
            readback, axis=CAP.axis, leaf=CAP.readback_leaf),
        status_projection=make_status_projection(CAP_FAMILY),
        name=CAP.adapter,
        binding_journal=bindings,
        # The scope the producer owns is the one in the policy body it stores.
        scope_key=lambda body: "/".join(
            f"{name}={body['config'][name]}" for name in CAP_FAMILY.scope_fields),
        refusal_errors=(A1Error, Campaign5BuilderError, ValueError),
        retain_binding_until_restore=True,
        clock=clock.now,
    )
    steering = MockActuationAdapter(config={STEERING_AXIS: HOME_NCI})
    steering.hosts_watchdogs = False
    transactions = (transaction_journal if transaction_journal is not None
                    else InMemoryTransactionJournal())
    gateway = TokenBoundWriteGateway(
        adapters={"mock": steering, CAP.adapter: cap_adapter},
        safe_state=baseline_config(),
        journal=transactions,
        axis_adapters={CAP.axis: CAP.adapter},
        clock=lambda: CLOCK,
    )
    return CapHarness(
        gateway=gateway, adapter=cap_adapter, steering=steering, port=port,
        producer=producer, kpm=stream, binding_journal=bindings,
        operation_journal=operations,
        transaction_journal=transactions, clock=clock,
        faults=faults or CapFaults(),
    )


def controlled_scope_builder():
    """The composition root's wrapper: build against the *controlled* UE scope.

    ``last_revision`` is forwarded rather than swallowed: the adapter seeds the
    builder from the durable binding journal, and a wrapper that dropped the
    keyword would silently restart the numbering at one on the far side of a
    restart -- the defect the seed exists to prevent.
    """
    build = make_policy_builder(
        CAP_FAMILY, validity_provider=lambda command: dict(VALIDITY))

    def build_for_controlled_ue(command: Mapping[str, Any], *,
                                last_revision: Optional[int] = None
                                ) -> Dict[str, Any]:
        scope = command.get("scope") or {}
        controlled = scope.get("controlledUe")
        if not isinstance(controlled, Mapping) or not controlled:
            raise ValueError("the plan scope carries no controlled UE")
        if all(controlled.get(name) == scope.get(name)
               for name in CAP_FAMILY.scope_fields):
            raise ValueError("the controlled UE is the objective UE")
        return build({**dict(command), "scope": dict(controlled),
                      "value": CAP.wire_value(command.get("value"))},
                     last_revision=last_revision)

    return build_for_controlled_ue


__all__ = [
    "APPLIED_CAP",
    "CAP",
    "CAP_FAMILY",
    "CLOCK",
    "CONTROLLED_UE",
    "CapFaults",
    "CapHarness",
    "ControlEvidence",
    "CountingCapPolicyPort",
    "EARLIER",
    "EXPIRED_LEASE",
    "HOME_NCI",
    "LEASE",
    "OBJECTIVE_UE",
    "STEERING_AXIS",
    "TARGET_NCI",
    "UNCAPPED",
    "A1Conflict",
    "applied_config",
    "baseline_config",
    "build_cap_harness",
    "controlled_scope_builder",
    "permit",
    "plan_scope",
]
