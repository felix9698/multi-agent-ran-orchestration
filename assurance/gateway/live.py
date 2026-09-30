"""Injected live binding for the R1 adapter; it never opens a transport itself."""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from assurance.gateway.r1_adapter import R1Adapter, R1PolicyPort, project_verified_readback
from assurance.contracts.live_binding import AssuranceLiveBinding, LiveR1Binding

__all__ = [
    "R1StatusPoller",
    "build_live_r1_adapter",
    "build_live_r1_adapter_from_transport",
    "build_live_r1_supplementary_adapter",
    "scalar_leaf_readback",
]


class R1StatusPoller:
    """Bounded R1 status GET sequencer derived from the deployment contract."""

    def __init__(self, policy_port: R1PolicyPort, *, cadence_ms: int, deadline_ms: int,
                 monotonic_ms: Callable[[], int], sleep_ms: Callable[[int], None],
                 projection: Callable[
                     [Mapping[str, Any]], Optional[Mapping[str, Any]]
                 ] = project_verified_readback) -> None:
        self._port, self._cadence_ms, self._deadline_ms = policy_port, cadence_ms, deadline_ms
        self._monotonic_ms, self._sleep_ms = monotonic_ms, sleep_ms
        # How a status becomes a configuration.  Injected because the surface a
        # plan moves is the deployment's, not this module's: the default reports
        # the frozen ``observedServingCell`` object as it stands, and a
        # deployment whose configuration axis is the cell identity alone
        # supplies the projection onto that axis instead.  The VERIFIED-only
        # rule is the projection's, and every projection keeps it.
        self._project = projection

    def readback(self, *, scope: Mapping[str, Any], transaction_id: str, policy_id: Optional[str]) -> Optional[Mapping[str, Any]]:
        del scope, transaction_id
        if policy_id is None:
            return None
        started = self._monotonic_ms()
        while self._monotonic_ms() - started <= self._deadline_ms:
            status = self._port.get_policy_status(policy_id)
            observed = self._project(status)
            if observed is not None:
                return observed
            aic = status.get("aicStatus") if isinstance(status, Mapping) else None
            if isinstance(aic, Mapping) and aic.get("episodeTerminal") is True:
                return None
            remaining = self._deadline_ms - (self._monotonic_ms() - started)
            if remaining <= 0:
                return None
            self._sleep_ms(min(self._cadence_ms, remaining))
        return None

    __call__ = readback


def build_live_r1_adapter(binding: AssuranceLiveBinding, *, policy_port: R1PolicyPort,
                          policy_builder: Callable[[Mapping[str, Any]], dict],
                          monotonic_ms: Callable[[], int], sleep_ms: Callable[[int], None],
                          status_projection: Callable[
                              [Mapping[str, Any]], Optional[Mapping[str, Any]]
                          ] = project_verified_readback,
                          readback_port: Optional[Any] = None,
                          binding_journal: Optional[Any] = None,
                          operation_journal: Optional[Any] = None,
                          scope_key: Optional[Callable[[Mapping[str, Any]], str]] = None,
                          refusal_errors: tuple = (),
                          retain_binding_until_restore: bool = False,
                          clock: Optional[Callable[[], str]] = None,
                          name: str = "r1",
                          ) -> R1Adapter:
    """Configure the existing adapter from data and an already-injected port.

    ``status_projection`` and ``readback_port`` are the two places a deployment
    states what *its* effect readback is.  The defaults are the bounded status
    poller over the frozen ``observedServingCell`` object, which is the right
    answer for a deployment whose configuration surface is that object.  A
    deployment that names its cells some other way -- or that can corroborate
    the producer's readback against a second, independent observation, which is
    what design section 9 wants and what an acceptance can never be -- supplies
    both here.  Neither may weaken the VERIFIED-only rule: a readback port that
    reported an acknowledgement as a configuration would make every
    ``ACK`` a success, which is the one thing this path exists to refuse.

    The journals, the scope key, the refusal types and the restore-retention
    flag are passed straight through to :class:`R1Adapter`.  They are optional
    here because the Gate 3 steering composition has always run without them,
    and named here because a *driver* that writes a policy over the air needs
    all four: a durable binding to name what it created after a restart, a
    durable operation record to say how many writes it caused, a producer
    refusal reported as a refusal rather than as a lost message, and a scope
    held until the baseline is read back rather than until the DELETE returns.

    ``name`` is what the adapter calls itself in its evidence references and in
    its operation journal.  It defaults to ``"r1"``, which is what the Gate 3
    steering composition has always been, and a route that registers the
    adapter under another key should pass that key: a journal entry naming an
    adapter the gateway does not know is a record nobody can join back up.
    """
    poller = R1StatusPoller(policy_port, cadence_ms=binding.r1.cadence_ms,
                            deadline_ms=binding.r1.deadline_ms, monotonic_ms=monotonic_ms,
                            sleep_ms=sleep_ms, projection=status_projection)
    return R1Adapter(policy_port=policy_port, policy_builder=policy_builder,
                     near_rt_ric_id=binding.r1.near_rt_ric_id,
                     policy_type_id=binding.r1.policy_type_id,
                     readback_port=poller if readback_port is None else readback_port,
                     status_projection=status_projection,
                     binding_journal=binding_journal,
                     operation_journal=operation_journal,
                     scope_key=scope_key,
                     refusal_errors=refusal_errors,
                     retain_binding_until_restore=retain_binding_until_restore,
                     clock=clock, name=name)


def build_live_r1_adapter_from_transport(
        binding: AssuranceLiveBinding, *,
        transport_factory: Callable[[LiveR1Binding], R1PolicyPort],
        policy_builder: Callable[[Mapping[str, Any]], dict],
        monotonic_ms: Callable[[], int], sleep_ms: Callable[[int], None],
        status_projection: Callable[
            [Mapping[str, Any]], Optional[Mapping[str, Any]]
        ] = project_verified_readback,
        readback_port: Optional[Any] = None) -> R1Adapter:
    """Bind an adapter through a caller-owned transport factory.

    The factory is the only place a real R1 client may be constructed.  Tests
    pass a fake here, while this module remains unable to open a socket itself.
    """
    return build_live_r1_adapter(
        binding, policy_port=transport_factory(binding.r1), policy_builder=policy_builder,
        monotonic_ms=monotonic_ms, sleep_ms=sleep_ms,
        status_projection=status_projection, readback_port=readback_port,
    )


def scalar_leaf_readback(
    readback: Any, *, axis: str, leaf: str,
    render: Callable[[Any], str] = str,
) -> Callable[..., Optional[Mapping[str, Any]]]:
    """Project a mapping-valued readback onto a scalar configuration axis.

    A family's contracted readback answers in the shape its *policy status*
    uses -- ``{"dlPrbCap": {"maxDlPrbs": 12}}`` -- while a deployment's
    configuration *axis* is a sortable scalar, exactly as the steering axis is
    the bare ``"87654321"`` string rather than the rich ``observedServingCell``
    object.  The two must be the same shape or no digest ever matches, and the
    scalar is the one that can also be a frozen candidate parameter: the Kernel
    refuses a plan whose steps do not literally realise the candidate the epoch
    froze, so an encoding between the two would be a place for a plan to drift
    from what it claims to be.

    ``None`` in is ``None`` out: this projects an observation, it never
    manufactures one, and a readback that could not observe stays unobserved.

    ``render`` spells the scalar the way the frozen axis does.  A real-valued
    axis is frozen as ``"4.0"`` while the producer may store the integral
    weight as ``4``; ``str`` would read that back as ``"4"``, a different
    digest for the configuration the radio really holds (2026-09-14 OTA:
    PF 4.0 applied and KPM-verified, Kernel PARTIAL_APPLY on ``"4"``).
    """

    def project(*, scope: Mapping[str, Any], transaction_id: str,
                policy_id: Optional[str]) -> Optional[Mapping[str, Any]]:
        observed = readback(
            scope=scope, transaction_id=transaction_id, policy_id=policy_id)
        if observed is None:
            return None
        value = observed.get(axis)
        if isinstance(value, Mapping):
            if leaf not in value:
                return None
            value = value[leaf]
        if value is None:
            return None
        return {axis: render(value)}

    return project


def build_live_r1_supplementary_adapter(
    *,
    adapter_key: str,
    policy_type_id: str,
    near_rt_ric_id: str,
    policy_port: R1PolicyPort,
    policy_builder: Callable[[Mapping[str, Any]], dict],
    readback_port: Any,
    scope_key: Callable[[Mapping[str, Any]], str],
    binding_journal: Any,
    status_projection: Callable[[Mapping[str, Any]], Optional[Mapping[str, Any]]],
    clock: Callable[[], str],
    refusal_errors: tuple = (),
    operation_journal: Any = None,
) -> R1Adapter:
    """Bind one SUPPLEMENTARY adapter to one fixed A1 policy type.

    ``adapter_key`` and ``policy_type_id`` are a *deployment* pair, read from
    the profile that named the action producer.  Neither can be selected from
    proposal text, and neither is mutated at runtime: ``r1-cap`` carries the
    deployment's UE cap type or it carries nothing.  The readback is the
    caller's corroborated port -- this function supplies no default, because a
    supplementary change that fell back to the steering status poller would
    report the wrong configuration surface and look verified doing it.

    ``binding_journal`` is mandatory here even though the primary adapter may
    run without one.  A supplementary policy is created *after* a primary one
    is already live, so an orphan on this side leaves a composition nobody can
    unwind; the durable binding is what makes the restore reachable after a
    restart.  ``retain_binding_until_restore`` is likewise fixed on: an A1
    DELETE only begins the restoration, and a DELETE response is not recovery.

    ``operation_journal`` is optional and durable when given: it records every
    policy-port call this adapter issues, so *how many writes a run caused* is
    read back after the fact from the deployment's own files rather than
    reconstructed from logs.
    """
    if binding_journal is None:
        raise ValueError(
            "a supplementary R1 adapter needs a durable binding journal; an "
            "in-memory binding would strand the policy a restart cannot name")
    if readback_port is None:
        raise ValueError(
            "a supplementary R1 adapter needs its own corroborated readback; "
            "an acknowledgement is not an effect")
    return R1Adapter(
        operation_journal=operation_journal,
        policy_port=policy_port,
        policy_builder=policy_builder,
        near_rt_ric_id=near_rt_ric_id,
        policy_type_id=policy_type_id,
        readback_port=readback_port,
        status_projection=status_projection,
        name=adapter_key,
        binding_journal=binding_journal,
        scope_key=scope_key,
        refusal_errors=tuple(refusal_errors),
        retain_binding_until_restore=True,
        clock=clock,
    )
