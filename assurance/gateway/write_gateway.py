"""The Write Gateway protocol and adapter registry.

Owner lane: **KGW**.  Signatures frozen by this design step.

Design section 4.4 makes this the only component allowed to perform dynamic
equipment changes, and section 9 fixes the one path it may use::

    Assurance Kernel -> Write Gateway -> R1 -> Non-RT RIC -> A1-P -> xApp
    -> FlexRIC -> E2SM -> OAI gNB

"The Write Gateway may orchestrate the registered O-RAN clients/adapters, but
it cannot silently replace the path with direct gNB control.  Direct SSH,
Telnet, OAI CLI, PRB/MCS/scheduler commands, process control, and USRP power
operations belong only to Lab Setup preparation, shutdown, or recovery."

That is why the registry below is keyed by
:class:`~assurance.contracts.capability.ActuatorPath` and why registering a
``LAB_SETUP_PREPARATION`` adapter is refused: the boundary is enforced where
adapters are admitted, not left to a reviewer noticing a new import.

Every operation takes a :class:`~assurance.gateway.token.KernelToken` and no
other authority.  The gateway performs no policy reasoning: it does not decide
whether a change is a good idea, whether a trial passed, or whether to retry.
It checks the permit, observes the expected configuration, acts, and reports
what it observed.

The **partial apply** case is the one that shapes the whole interface.  A
change can succeed at R1, be accepted at A1-P, and still not reach the gNB; or
reach it and not be readable back.  So every mutating operation returns a
:class:`GatewayOutcome` that can say ``PARTIAL_APPLY`` or ``UNKNOWN``, and
neither is convertible to "no change happened".  Design section 8 requires the
Kernel to query, not assume, and it can only query if the gateway is honest
about not knowing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional, Protocol, Tuple, runtime_checkable

from assurance.contracts.capability import ActuatorPath
from assurance.gateway.token import KernelToken

__all__ = [
    "AdapterRegistry",
    "GatewayOutcome",
    "GatewayRefusal",
    "GatewayResult",
    "HOSTS_WATCHDOGS_ATTRIBUTE",
    "WriteGateway",
    "WriteGatewayAdapter",
]


class GatewayOutcome(Enum):
    """What the gateway observed after an operation.

    ``PARTIAL_APPLY`` and ``UNKNOWN`` are distinct and neither means "nothing
    happened".  ``PARTIAL_APPLY`` is a positively observed inconsistency --
    some of the change is live and some is not.  ``UNKNOWN`` is the honest
    answer when the acknowledgement was lost: the change may or may not be
    live, and only a configuration reread can say.
    """

    #: Applied and read back as the expected configuration.
    ACKED = "ACKED"
    #: Refused before any change: the request was not admissible downstream.
    REJECTED = "REJECTED"
    #: A newer fencing token has superseded this one.
    REJECTED_FENCE = "REJECTED_FENCE"
    #: The lease had expired when the command arrived.
    REJECTED_LEASE_EXPIRED = "REJECTED_LEASE_EXPIRED"
    #: The observed configuration did not match ``expected_config_hash``.
    REJECTED_CONFIG_MISMATCH = "REJECTED_CONFIG_MISMATCH"
    #: Same idempotency key, different content.
    REJECTED_IDEMPOTENCY_COLLISION = "REJECTED_IDEMPOTENCY_COLLISION"
    #: Recognised as a retransmission of an already-applied command; no
    #: second effect was produced.
    ALREADY_APPLIED = "ALREADY_APPLIED"
    #: Observed inconsistency: part of the change is live.
    PARTIAL_APPLY = "PARTIAL_APPLY"
    #: The acknowledgement was lost; liveness is undetermined.
    UNKNOWN = "UNKNOWN"
    #: The transport or the downstream component errored.
    ERROR = "ERROR"


@dataclass(frozen=True)
class GatewayResult:
    """What one gateway operation observed.

    Attributes
    ----------
    outcome:
        See :class:`GatewayOutcome`.
    observed_config_hash:
        Digest of the configuration the gateway actually read back, or
        ``None`` when it could not read.  Distinguishing "read and it differs"
        from "could not read" is the difference between a rejection and a
        recovery.
    observed_config:
        The axes this adapter itself observed, when it can name them.  A
        deployment reached over one client reports the whole configuration
        surface and its digest says everything; a composition reached over two
        -- a steering policy on one, a UE cap on another -- has no single client
        that can see the whole surface, so each reports *its* axes and the
        gateway merges them before hashing.  ``None`` keeps the
        single-participant behaviour exactly as it was: the digest is the
        answer.
    evidence_refs:
        References to the raw request/response records this operation
        produced.  Design section 9: an A1 create or an E2 ACK is not success
        on its own, but it is evidence and it is preserved.
    detail:
        Short diagnostic for the Cockpit.  Display only; it never
        participates in a verdict, and it must never contain a credential.
    """

    outcome: GatewayOutcome
    observed_config_hash: Optional[str] = None
    evidence_refs: Tuple[str, ...] = ()
    detail: str = ""
    observed_config: Optional[Mapping[str, Any]] = None


#: Name of the optional adapter attribute declaring watchdog hosting.  See
#: :class:`WriteGatewayAdapter`.  Read with a ``False`` default, never
#: required: an adapter that does not host is the ordinary case.
HOSTS_WATCHDOGS_ATTRIBUTE = "hosts_watchdogs"


class GatewayRefusal(RuntimeError):
    """The gateway refused to act on a token.

    Raised for a malformed or unauthorised permit -- wrong
    :class:`~assurance.gateway.token.TokenKind`, expired lease, absent adapter
    -- as opposed to a downstream refusal, which is a
    :class:`GatewayResult` with a ``REJECTED*`` outcome.  The distinction
    matters on recovery: a refusal here means nothing reached the equipment.
    """


@runtime_checkable
class WriteGatewayAdapter(Protocol):
    """One registered O-RAN client behind the gateway.

    An adapter speaks a transport -- R1, A1-P, O1 -- and knows nothing about
    trials, harm or verdicts.  It is handed an already-validated command and
    reports what it observed.

    **Optional declaration:** :data:`HOSTS_WATCHDOGS_ATTRIBUTE`.  An adapter
    may set ``hosts_watchdogs = True`` to say that the deployment behind it can
    host a contract watchdog -- arm it before the commit line and act on it
    without the Kernel.  Only the adapter knows this: an A1 policy path has no
    command that installs a guard ahead of the policy it guards, while a direct
    scheduler control does.

    It is deliberately *not* a protocol member, because the absent case is
    meaningful and safe rather than malformed.  Absent, it reads ``False``: the
    gateway sends no arming command and the Kernel arms the contract watchdog
    on its own two mechanisms instead (measurement staleness and the permit
    lease deadline; ``docs/architecture/SEAMS-GATE2.md`` section 8.3).  An
    adapter that could host but forgot to say so loses strength, never safety.
    """

    #: The path this adapter serves.  Only
    #: :attr:`~assurance.contracts.capability.ActuatorPath.OFFICIAL_ORAN_DYNAMIC`
    #: adapters may be registered on the objective path.
    actuator_path: ActuatorPath

    def dispatch(
        self, *, token: KernelToken, command: Mapping[str, Any]
    ) -> GatewayResult:
        """Perform one already-validated command; report what was observed.

        Signature frozen; body owned by lane **KGW**.

        Must not retry on its own.  Retry policy is a Kernel decision with
        harm and lease consequences, and a silent adapter-level retry would
        apply a change twice under one idempotency key.
        """
        ...


class AdapterRegistry:
    """The set of adapters the gateway may dispatch through.

    Registration is where design section 9's boundary is enforced.  A
    ``LAB_SETUP_PREPARATION`` adapter is refused here rather than filtered
    later, because "filtered later" is how a direct-control path ends up one
    forgotten branch away from an objective effect.
    """

    def register(self, name: str, adapter: WriteGatewayAdapter) -> None:
        """Register *adapter* under *name*.

        Signature frozen; body owned by lane **KGW**.

        Must refuse an adapter whose
        :attr:`~assurance.gateway.write_gateway.WriteGatewayAdapter.actuator_path`
        is not
        :attr:`~assurance.contracts.capability.ActuatorPath.OFFICIAL_ORAN_DYNAMIC`,
        and must refuse re-registering a name already in use -- a silently
        replaced adapter would change where every subsequent command went.
        """
        raise NotImplementedError("owned by lane KGW; see docs/architecture/SEAMS-GATE2.md")

    def resolve(self, name: str) -> WriteGatewayAdapter:
        """Return the adapter registered under *name*.

        Signature frozen; body owned by lane **KGW**.

        Raises :class:`GatewayRefusal` when absent.  Returning ``None`` would
        put the check at every call site instead of here.
        """
        raise NotImplementedError("owned by lane KGW; see docs/architecture/SEAMS-GATE2.md")

    def registered_paths(self) -> Mapping[str, ActuatorPath]:
        """Adapter name to actuator path, for the boundary tests.

        Signature frozen; body owned by lane **KGW**.
        """
        raise NotImplementedError("owned by lane KGW; see docs/architecture/SEAMS-GATE2.md")


class WriteGateway(Protocol):
    """The only component that performs dynamic equipment changes.

    Every method takes a :class:`~assurance.gateway.token.KernelToken` of the
    matching :class:`~assurance.gateway.token.TokenKind` and must verify, in
    this order, before touching anything:

    1. the token authorises this operation
       (:meth:`~assurance.gateway.token.KernelToken.authorises`);
    2. the lease has not expired
       (:meth:`~assurance.gateway.token.KernelToken.is_expired`);
    3. no newer fencing token has superseded it
       (:meth:`~assurance.gateway.token.KernelToken.fences_out`);
    4. the observed configuration matches ``expected_config_hash``;
    5. the idempotency key has not been used for different content.

    A failure at any step is a result, not an exception, except where nothing
    reached the equipment -- see :class:`GatewayRefusal`.
    """

    def prepare(
        self, *, token: KernelToken, plan: Mapping[str, Any]
    ) -> GatewayResult:
        """Validate and stage *plan* **without any side effect**.

        Signature frozen; body owned by lane **KGW**.

        Task section 6.2: no real configuration changes before READY.  Prepare
        may resolve bindings, check capability constraints, build the policy
        body and reserve local resources; it may not create an A1 policy, send
        an E2 command or write anything to the equipment.

        A prepare failure ends the trial in ``PRE_COMMIT_ABORT`` with nothing
        to roll back, which is only true if this method really has no effect.
        """
        ...

    def ready(self, *, token: KernelToken) -> GatewayResult:
        """Confirm durable ready state (design section 7 step 6).

        Signature frozen; body owned by lane **KGW**.

        "Durable" means the staged plan survives a gateway restart.  The
        Kernel records ``COMMIT_DECIDED`` on the strength of this answer, so a
        ready that is only in memory would make the commit decision a promise
        the gateway cannot keep.
        """
        ...

    def commit(self, *, token: KernelToken) -> GatewayResult:
        """Apply the staged change through the official O-RAN path.

        Signature frozen; body owned by lane **KGW**.

        The first real state-changing operation; the Kernel's trial count and
        harm clock start from it exactly once (task section 6.4).

        Must report ``PARTIAL_APPLY`` when it observes an inconsistent result
        and ``UNKNOWN`` when the acknowledgement is lost.  It must never
        convert either into ``REJECTED``: that would tell the Kernel nothing
        happened when something may have.
        """
        ...

    def stop(self, *, token: KernelToken) -> GatewayResult:
        """Halt the change immediately.

        Signature frozen; body owned by lane **KGW**.

        A safety action that outranks any semantic evaluation (design section
        7).  It must not wait for an agent, a predicate evaluator or a
        measurement window, and it must be safe to call from any post-commit
        state including one already stopping.
        """
        ...

    def reverse_rollback(self, *, token: KernelToken) -> GatewayResult:
        """Reverse the applied change in the opposite order it was applied.

        Signature frozen; body owned by lane **KGW**.

        Order matters: undoing a multi-step change forwards can pass through a
        configuration that was never valid.  Every post-commit non-success
        path runs through here before another trial may start (design section
        7).
        """
        ...

    def reread_configuration(self, *, token: KernelToken) -> GatewayResult:
        """Read the live configuration back without changing it.

        Signature frozen; body owned by lane **KGW**.

        The answer to ``UNKNOWN``.  Returns the observed configuration hash so
        the Kernel can decide whether the change is live, absent or partial --
        design section 9 requires "contracted actual effect readback", not an
        acceptance acknowledgement.
        """
        ...

    def confirm_recovery(self, *, token: KernelToken) -> GatewayResult:
        """Confirm the deployment is in a known safe state.

        Signature frozen; body owned by lane **KGW**.

        Design section 8 blocks new trials until every uncertain transaction is
        resolved; a successful result here is one of the four resolutions.
        Must fail rather than assume when the state cannot be established --
        an unfounded confirmation would release the block that exists to stop
        the next trial running on top of an unknown configuration.
        """
        ...

    def emergency_safe_state(self, *, token: KernelToken) -> GatewayResult:
        """Drive the deployment to its contracted safe state.

        Signature frozen; body owned by lane **KGW**.

        The endpoint of Emergency Stop, which flows Kernel ``OPERATOR_ABORT``
        -> gateway stop -> rollback -> recovery (design section 11).  It must
        work from any state, including one where prepare never completed.
        """
        ...

    def finalize_live(self, *, token: KernelToken) -> GatewayResult:
        """Make a successful change the live configuration.

        Signature frozen; body owned by lane **KGW**.

        Called only after the Kernel has a durable success decision and a
        configuration reread (task section 6.9).  The acknowledgement returned
        here is what permits the final success settlement; without it the
        trial is not a deployed success no matter what the predicates said.
        """
        ...

    def query_transaction(self, transaction_id: str) -> GatewayResult:
        """Report the durable state of *transaction_id*.

        Signature frozen; body owned by lane **KGW**.

        The recovery query.  Must answer from the gateway's own durable
        record -- transaction phase, fencing token, configuration snapshots,
        reservations, participant state and next safe action (design section
        8) -- and must answer ``UNKNOWN`` rather than guess.
        """
        ...
