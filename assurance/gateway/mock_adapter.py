"""An in-memory actuation adapter with injectable failures.

Owner lane: **KGW**.

Gate 2 is hardware-free (design section 15: zero live E2/RAN/OTA/USRP calls),
so the gateway's behaviour has to be provable against something that can fail
the way real equipment fails.  This adapter is that something: a configuration
store in a dictionary, a log of every command it received, and a
:class:`FaultInjection` record describing which failures to produce.

The injectable failures are chosen from the ones the design names as decisive,
not from the ones that are easy to simulate:

``fail_prepare``
    Prepare is refused downstream.  The trial must end in a pre-commit abort
    with nothing to roll back, which is only true if prepare really wrote
    nothing -- and :attr:`writes` is how that is checked rather than asserted.
``fail_axes``
    One axis of a multi-axis change is refused.  The earlier axes are live and
    the later ones are not: a partial apply, which the gateway must observe by
    readback rather than infer from the acknowledgements.
``drop_ack_axes``
    The write lands and the acknowledgement does not.  The honest answer is
    ``UNKNOWN``, and only a configuration reread can resolve it (design
    section 8).
``unreadable`` / ``unreadable_after_reads``
    The configuration cannot be read, either from the start or after a given
    number of further reads.  This is what makes an uncertain transaction stay
    uncertain instead of being confirmed on no evidence, and what lets a test
    lose an acknowledgement *and* the readback that would have resolved it.
``crash_axes``
    The process dies mid-apply: the adapter raises ``KeyboardInterrupt``, which
    the gateway does not catch, so nothing reconciles the transaction.  This is
    the case the durable ``APPLYING`` record exists for.
``drift``
    An out-of-band change to the live configuration between operations, which
    is what a stale ``expected_config_hash`` is there to catch.
``latency_s``
    Reported through an injected sleep function so a test can assert the delay
    was requested without a test suite that actually waits.

The adapter never retries: retry policy is a Kernel decision with harm and
lease consequences, and a silent adapter-level retry would apply a change twice
under one idempotency key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional

from assurance.contracts.capability import ActuatorPath
from assurance.gateway.commands import GatewayOperation
from assurance.gateway.plan import config_hash
from assurance.gateway.token import KernelToken
from assurance.gateway.write_gateway import GatewayOutcome, GatewayResult

__all__ = ["FaultInjection", "MockActuationAdapter"]


@dataclass(frozen=True)
class FaultInjection:
    """Which failures this adapter should produce.  Empty means none."""

    fail_prepare: bool = False
    #: Axes whose write is refused outright, with no effect.
    fail_axes: FrozenSet[str] = frozenset()
    #: Axes whose write lands but whose acknowledgement is lost.
    drop_ack_axes: FrozenSet[str] = frozenset()
    #: Axes whose reversal fails.
    fail_undo_axes: FrozenSet[str] = frozenset()
    #: The live configuration cannot be read at all.
    unreadable: bool = False
    #: Reads succeed this many times after the faults are installed, then fail.
    unreadable_after_reads: Optional[int] = None
    #: Axes whose write kills the process instead of returning.
    crash_axes: FrozenSet[str] = frozenset()
    #: Finalize is not acknowledged.
    drop_finalize_ack: bool = False
    #: Watchdog ids whose arming is refused.  The Kernel must then refuse the
    #: durable commit decision rather than apply an unguarded change.
    fail_arm_watchdogs: FrozenSet[str] = frozenset()
    #: An out-of-band configuration change, applied once at construction.
    drift: Mapping[str, Any] = field(default_factory=dict)
    #: Seconds of injected latency, reported through the injected sleep.
    latency_s: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "fail_axes", frozenset(self.fail_axes))
        object.__setattr__(self, "drop_ack_axes", frozenset(self.drop_ack_axes))
        object.__setattr__(self, "fail_undo_axes", frozenset(self.fail_undo_axes))
        object.__setattr__(self, "crash_axes", frozenset(self.crash_axes))
        object.__setattr__(
            self, "fail_arm_watchdogs", frozenset(self.fail_arm_watchdogs)
        )
        object.__setattr__(self, "drift", dict(self.drift))


class MockActuationAdapter:
    """A configuration store that answers gateway commands.

    It names the axes it observed on every read (``observed_config``), which is
    what lets it stand in for *one participant* of a multi-adapter composition:
    a mock built with only the steering axis reports only that axis, and the
    gateway merges it with whatever the other client observed.  A mock built
    with the whole surface behaves exactly as it always did.

    It is a mock and it says so.  Nothing here derives a live badge, a live
    evidence source or an OTA level; a run over this adapter is
    ``HARDWARE_FREE_ROUND_TRIP`` and the Kernel's evidence state says so.
    """

    #: Registered on the objective path; a Lab Setup adapter would be refused
    #: at registration (design section 9).
    actuator_path = ActuatorPath.OFFICIAL_ORAN_DYNAMIC

    #: This deployment can install a guard before the change it guards, so the
    #: gateway arms contract watchdogs on it directly.
    hosts_watchdogs = True

    def __init__(
        self,
        *,
        config: Mapping[str, Any],
        faults: Optional[FaultInjection] = None,
        sleep: Optional[Callable[[float], None]] = None,
        name: str = "mock",
    ) -> None:
        self._config: Dict[str, Any] = dict(config)
        self._config.update(dict((faults or FaultInjection()).drift))
        self.faults = faults or FaultInjection()
        self.name = name
        #: Every command received, in order.
        self.commands: List[Dict[str, Any]] = []
        #: Every command that changed the store.  Empty after a prepare.
        self.writes: List[Dict[str, Any]] = []
        #: Derived idempotency keys seen, in order.  A duplicate here would
        #: mean the gateway sent one effect twice.
        self.idempotency_keys: List[str] = []
        #: Latencies the adapter asked to wait for.
        self.delays: List[float] = []
        #: Watchdog ids this adapter reports as armed, in arming order.
        self.armed_watchdogs: List[str] = []
        self._sleep = sleep if sleep is not None else self.delays.append
        self._dispatches = 0
        self._reads = 0

    # -- inspection --------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """The live configuration, for assertions."""
        return dict(self._config)

    def live_hash(self) -> str:
        """Digest of the live configuration."""
        return config_hash(self._config)

    def set_faults(self, faults: FaultInjection) -> None:
        """Change the injected failures between operations.

        Resets the read counter, so ``unreadable_after_reads`` means "this many
        more reads work" and a test can say that where it is readable rather
        than by counting the gateway's internal reads.
        """
        self.faults = faults
        self._reads = 0

    def apply_drift(self, drift: Mapping[str, Any]) -> None:
        """Change the live configuration out of band, as the field can."""
        self._config.update(dict(drift))

    # -- the frozen adapter surface ---------------------------------------

    def dispatch(
        self, *, token: KernelToken, command: Mapping[str, Any]
    ) -> GatewayResult:
        """Perform one already-validated command; report what was observed."""
        operation = GatewayOperation(command["operation"])
        self.commands.append(dict(command))
        self.idempotency_keys.append(command["idempotencyKey"])
        self._dispatches += 1
        reference = f"{self.name}:{self._dispatches}"
        if self.faults.latency_s:
            self._sleep(self.faults.latency_s)
        if operation is GatewayOperation.VALIDATE:
            if self.faults.fail_prepare:
                return GatewayResult(
                    outcome=GatewayOutcome.REJECTED,
                    evidence_refs=(reference,),
                    detail="injected prepare failure",
                )
            return self._read(reference, "validated without writing")
        if operation is GatewayOperation.READ:
            return self._read(reference, "configuration read back")
        if operation is GatewayOperation.APPLY:
            return self._write(command, reference)
        if operation is GatewayOperation.UNDO:
            axis = command["axis"]
            if axis in self.faults.fail_undo_axes:
                return GatewayResult(
                    outcome=GatewayOutcome.ERROR,
                    evidence_refs=(reference,),
                    detail=f"injected reversal failure on {axis}",
                )
            return self._write(command, reference)
        if operation is GatewayOperation.HALT:
            self.writes.append(dict(command))
            return GatewayResult(
                outcome=GatewayOutcome.ACKED, evidence_refs=(reference,), detail="halted"
            )
        if operation is GatewayOperation.ARM_WATCHDOG:
            watchdog_id = command["watchdogId"]
            if watchdog_id in self.faults.fail_arm_watchdogs:
                return GatewayResult(
                    outcome=GatewayOutcome.REJECTED,
                    evidence_refs=(reference,),
                    detail=f"injected arming failure on {watchdog_id}",
                )
            self.armed_watchdogs.append(watchdog_id)
            # The evidence reference the Kernel checks against its own
            # epoch-frozen watchdog set; the deployment reports the arming,
            # the Kernel never asserts it.
            return GatewayResult(
                outcome=GatewayOutcome.ACKED,
                evidence_refs=(f"watchdog:{watchdog_id}:armed", reference),
                detail=f"armed {watchdog_id}",
            )
        if operation is GatewayOperation.FINALIZE:
            if self.faults.drop_finalize_ack:
                return GatewayResult(
                    outcome=GatewayOutcome.UNKNOWN,
                    evidence_refs=(reference,),
                    detail="finalize acknowledgement lost",
                )
            return self._read(reference, "finalize acknowledged")
        return GatewayResult(
            outcome=GatewayOutcome.ERROR,
            evidence_refs=(reference,),
            detail=f"unsupported operation {operation.value}",
        )

    # -- internals ---------------------------------------------------------

    def _read(self, reference: str, detail: str) -> GatewayResult:
        served, self._reads = self._reads, self._reads + 1
        exhausted = (
            self.faults.unreadable_after_reads is not None
            and served >= self.faults.unreadable_after_reads
        )
        if self.faults.unreadable or exhausted:
            return GatewayResult(
                outcome=GatewayOutcome.ERROR,
                evidence_refs=(reference,),
                detail="injected read failure",
            )
        return GatewayResult(
            outcome=GatewayOutcome.ACKED,
            observed_config_hash=self.live_hash(),
            observed_config=dict(self._config),
            evidence_refs=(reference,),
            detail=detail,
        )

    def _write(self, command: Mapping[str, Any], reference: str) -> GatewayResult:
        axis, value = command["axis"], command["value"]
        operation = GatewayOperation(command["operation"])
        if operation is GatewayOperation.APPLY and axis in self.faults.crash_axes:
            # Not an outcome: the process goes away.  The gateway catches
            # Exception, never BaseException, so the transaction is left in its
            # durable APPLYING phase exactly as a real crash would leave it.
            raise KeyboardInterrupt(f"simulated process death writing {axis}")
        if operation is GatewayOperation.APPLY and axis in self.faults.fail_axes:
            return GatewayResult(
                outcome=GatewayOutcome.REJECTED,
                evidence_refs=(reference,),
                detail=f"injected refusal on {axis}",
            )
        self._config[axis] = value
        self.writes.append(dict(command))
        if operation is GatewayOperation.APPLY and axis in self.faults.drop_ack_axes:
            # The write landed; the acknowledgement did not.  Reporting
            # anything but UNKNOWN here would be a lie the gateway would
            # faithfully pass on.
            return GatewayResult(
                outcome=GatewayOutcome.UNKNOWN,
                evidence_refs=(reference,),
                detail=f"acknowledgement lost writing {axis}",
            )
        return GatewayResult(
            outcome=GatewayOutcome.ACKED,
            observed_config_hash=self.live_hash(),
            observed_config=dict(self._config),
            evidence_refs=(reference,),
            detail=f"{operation.value.lower()} {axis}",
        )

