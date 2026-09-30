"""Wire a Campaign 5 family onto the Write Gateway's official O-RAN path.

The target diagram (design section 4.1)::

    XAppExecutionPlan -> Kernel admission -> permit
      -> ActuationPlan(adapter="r1-<family>", steps=[(axis, value)])
      -> TokenBoundWriteGateway.prepare/ready/commit
      -> R1Adapter.dispatch -> policy_builder(command) -> A1 policy
      -> A1-P Producer -> FlexRIC xApp -> E2SM-RC Control -> gNB
      -> readback_port <- E2SM-KPM counter

There is no new adapter class: the switch is one ``R1Adapter`` per family, each
carrying its own ``policy_type_id``, ``policy_builder``, ``policy_port`` and
``readback_port``.  The specialist xApp executors keep planning
(ownership/conflicts/order/holds/rollback in ``assurance/xapps/execution.py``);
they lose only the write, which now goes through the gateway.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from assurance.contracts.capability import DeploymentBinding, TransportSecurity
from assurance.contracts.live_binding import AssuranceLiveBinding, LiveR1Binding
from assurance.gateway.gateway import TokenBoundWriteGateway
from assurance.gateway.live import build_live_r1_adapter
from assurance.gateway.plan import config_hash

from oran.campaign5.builders import make_policy_builder
from oran.campaign5.families import (
    CAMPAIGN5_FAMILIES,
    Campaign5Family,
    Campaign5Error,
    campaign5_capability_manifest,
    verify_campaign5_discovery,
)
from oran.campaign5.readback import (
    AbsentCounterReader,
    CorroboratedConfigReadback,
    KpmConfigReader,
    PowerReadbackUnavailable,
    make_status_projection,
)

__all__ = [
    "RC_STYLE2_FAMILY_KEYS",
    "build_official_adapter",
    "family_scope_key",
    "build_official_gateway",
    "campaign5_live_binding",
    "family_for_rc_style2",
    "official_plan",
    "rc_style2_official_plan",
    "run_official_route",
]

#: The three Style-2 UE/cell declarations in ``rc_style2_actions.py`` and the
#: Campaign 5 family each maps onto.  ``dl-rf-attenuation`` (power) is not in
#: that declaration set -- it is fully custom (design section 4.4 level 4) and is
#: routed directly, not through the Style-2 declaration layer.
RC_STYLE2_FAMILY_KEYS: Dict[str, str] = {
    "ue-dl-prb-cap": "cap",
    "scheduler-priority": "priority",
    "dl-mcs-bounds": "mcs",
}


class _CapabilityGatedPolicyPort:
    """Enforce Campaign 5 discovery before the adapter can build a policy.

    The gateway invokes ``get_policy_type`` during ``prepare``/``VALIDATE``.
    Keeping the check on that injected port makes the three-way schema and
    RAN-definition gate part of the actual write route, rather than a helper
    that only hermetic unit tests happen to call.
    """

    def __init__(self, port: Any, family: Campaign5Family,
                 capability_manifest: Mapping[str, Any]) -> None:
        self._port = port
        self._family = family
        self._manifest = capability_manifest

    def get_policy_type(self, policy_type_id: str) -> Mapping[str, Any]:
        detail = self._port.get_policy_type(policy_type_id)
        verify_campaign5_discovery(
            {policy_type_id: detail}, self._manifest, self._family.policy_type_id
        )
        return detail

    def __getattr__(self, name: str) -> Any:
        return getattr(self._port, name)


def _deployment(endpoint_id: str, base_url: str) -> DeploymentBinding:
    return DeploymentBinding(
        contract_id=f"deployment/{endpoint_id}", version="1.0.0",
        schema_version="assurance/1.0.0", document_status="NORMATIVE",
        standard_mapping={"O-RAN": endpoint_id}, endpoint_id=endpoint_id,
        base_url=base_url, transport_security=TransportSecurity.MTLS,
        secret_refs={},
    )


def campaign5_live_binding(
    family: Campaign5Family,
    *,
    api_root: str = "https://near-rt.invalid/r1",
    near_rt_ric_id: str = "nearRT-RIC",
    cadence_ms: int = 200,
    deadline_ms: int = 4000,
) -> AssuranceLiveBinding:
    """A minimal binding carrying the family's R1 policy type for the adapter.

    Only ``binding.r1`` is read by ``build_live_r1_adapter``; the remaining
    fields are placeholders so a deployment can hand a fully-loaded binding
    while a test hands this one.
    """
    r1 = LiveR1Binding(
        api_root=api_root, near_rt_ric_id=near_rt_ric_id,
        policy_type_id=family.policy_type_id, cadence_ms=cadence_ms,
        deadline_ms=deadline_ms, deployment=_deployment("r1", api_root),
    )
    placeholder = _deployment("a1p", "https://a1p.invalid")
    return AssuranceLiveBinding(
        binding_id=f"campaign5-{family.key}", r1=r1, a1p=placeholder,
        o1=_deployment("o1", "https://o1.invalid"), o1_netconf="", o1_sftp="",
        pm_directory="", kpm_jsonl_path="", kpm_expected_epochs={},
        e2_nodes=(), cells=(), plmn={"mcc": "001", "mnc": "01"},
        source_digests={},
    )


def build_official_adapter(
    family: Campaign5Family,
    *,
    policy_port: Any,
    validity_provider: Callable[[Mapping[str, Any]], Mapping[str, str]],
    kpm_reader: Optional[KpmConfigReader] = None,
    monotonic_ms: Optional[Callable[[], int]] = None,
    sleep_ms: Optional[Callable[[int], None]] = None,
    cadence_ms: int = 200,
    deadline_ms: int = 4000,
    api_root: str = "https://near-rt.invalid/r1",
    near_rt_ric_id: str = "nearRT-RIC",
    capability_manifest: Optional[Mapping[str, Any]] = None,
    binding_journal: Optional[Any] = None,
    operation_journal: Optional[Any] = None,
    refusal_errors: Tuple[type, ...] = (),
    retain_binding_until_restore: bool = False,
    clock: Optional[Callable[[], str]] = None,
    cell_power_reader: bool = False,
) -> Any:
    """Build the family's ``R1Adapter`` with its builder and corroborated readback.

    ``kpm_reader`` defaults to :class:`AbsentCounterReader` -- the honest state
    of the deployed binary today, where the configuration counter is unbuilt and
    every readback therefore answers ``UNKNOWN`` rather than a fabricated effect.
    Power is stricter: its current three-component KPM counter loses component
    labels in JSONL, so it remains unavailable even if a generic reader is
    injected, until that external wire dependency is supplied.

    ``binding_journal`` / ``operation_journal`` / ``refusal_errors`` /
    ``retain_binding_until_restore`` are what a *live driver* supplies and a
    hermetic route test does not.  The scope key is derived here rather than
    taken from the caller: the scope a Campaign 5 producer owns is the one in
    the policy body it stores, and it is exactly the family's identity leaves.
    """
    binding = campaign5_live_binding(
        family, api_root=api_root, near_rt_ric_id=near_rt_ric_id,
        cadence_ms=cadence_ms, deadline_ms=deadline_ms,
    )
    ticks = [0]

    def _default_monotonic() -> int:
        ticks[0] += cadence_ms
        return ticks[0]

    monotonic = monotonic_ms or _default_monotonic
    sleeper = sleep_ms or (lambda _ms: None)
    reader: KpmConfigReader = (
        # 2026-09-25: power reads back only through a cell-scoped reader the caller vouches
        # for (cell_power_reader=True: live_run's KpmFamilyConfigReader, the same scalar
        # RAN.Cell.TxAttenuationDb the board path reads since 09-16).  A generic reader never
        # may -- it would fabricate a configuration (Codex/full-suite review 2026-09-26).
        PowerReadbackUnavailable()
        if family.key == "power" and not (cell_power_reader and kpm_reader is not None)
        else kpm_reader if kpm_reader is not None else AbsentCounterReader()
    )
    gated_port = _CapabilityGatedPolicyPort(
        policy_port, family,
        campaign5_capability_manifest()
        if capability_manifest is None else capability_manifest,
    )
    readback = CorroboratedConfigReadback(
        family, status_port=gated_port, kpm_reader=reader,
        monotonic_ms=monotonic, sleep_ms=sleeper,
        cadence_ms=cadence_ms, deadline_ms=deadline_ms,
    )
    return build_live_r1_adapter(
        binding, policy_port=gated_port,
        policy_builder=make_policy_builder(family, validity_provider=validity_provider),
        monotonic_ms=monotonic, sleep_ms=sleeper,
        status_projection=make_status_projection(family),
        readback_port=readback,
        binding_journal=binding_journal,
        operation_journal=operation_journal,
        scope_key=(None if binding_journal is None
                   else lambda body: family_scope_key(family, body)),
        refusal_errors=refusal_errors,
        retain_binding_until_restore=retain_binding_until_restore,
        clock=clock,
        # The gateway registers this adapter under ``r1-<family>``; its own
        # records say the same thing, so an evidence reference and a journal
        # entry can be joined back to the participant that made them.
        name=family.adapter_name,
    )


def family_scope_key(family: Campaign5Family, body: Mapping[str, Any]) -> str:
    """The semantic scope a Campaign 5 producer owns, read off the policy body.

    The body, not the plan scope: the producer admits one non-terminal policy
    per ``(policyTypeId, semanticScopeKey)`` and the key it enforces is the one
    in the object it stored.  Deriving it from the plan would key the durable
    binding off something the producer never saw.
    """
    config = body.get("config") or {}
    return "/".join(f"{name}={config[name]}" for name in family.scope_fields)


def build_official_gateway(
    family: Campaign5Family,
    adapter: Any,
    *,
    safe_state: Mapping[str, Any],
    clock: Callable[[], str],
    journal: Any = None,
) -> TokenBoundWriteGateway:
    """Register the family adapter under ``r1-<family>`` and refuse anything else.

    ``GatewayAdapterRegistry.register`` accepts only
    ``ActuatorPath.OFFICIAL_ORAN_DYNAMIC``; the ``R1Adapter`` declares it, so the
    lab telnet path (``LAB_SETUP_PREPARATION``) cannot be registered here.
    """
    return TokenBoundWriteGateway(
        adapters={family.adapter_name: adapter},
        safe_state=dict(safe_state), journal=journal, clock=clock,
    )


def official_plan(
    family: Campaign5Family,
    *,
    scope: Mapping[str, Any],
    baseline: Mapping[str, Any],
    target: Mapping[str, Any],
) -> Dict[str, Any]:
    """Build the single-axis ``ActuationPlan`` mapping for one family change.

    ``scope`` carries the family's identity leaves (cell for mcs/power, cell+UE
    for cap/priority); ``baseline`` and ``target`` are the value leaves the plan
    step moves.  The plan surface is the one contracted axis ``family.axis``.
    """
    missing_scope = [f for f in family.scope_fields if f not in scope]
    if missing_scope:
        raise Campaign5Error(f"{family.key}: scope missing {missing_scope}")
    for label, value in (("baseline", baseline), ("target", target)):
        keys = set(value)
        if keys != set(family.value_fields):
            raise Campaign5Error(
                f"{family.key}: {label} must be exactly {family.value_fields}, got {sorted(keys)}"
            )
    return {
        "adapter": family.adapter_name,
        "scope": {f: scope[f] for f in family.scope_fields},
        "baselineConfig": {family.axis: dict(baseline)},
        "steps": [{"axis": family.axis, "value": dict(target)}],
    }


def family_for_rc_style2(action_key: str) -> Campaign5Family:
    """Map an ``rc_style2_actions`` declaration key onto its Campaign 5 family."""
    try:
        return CAMPAIGN5_FAMILIES[RC_STYLE2_FAMILY_KEYS[action_key]]
    except KeyError as exc:
        raise Campaign5Error(
            f"{action_key} is not a Style-2 UE/cell declaration with an official route"
        ) from exc


def rc_style2_official_plan(
    action_key: str,
    *,
    scope: Mapping[str, Any],
    baseline: Mapping[str, Any],
    target: Mapping[str, Any],
) -> Tuple[Campaign5Family, Dict[str, Any]]:
    """The runtime consumer of ``rc_style2_actions``: declaration -> official plan.

    This is the seam ``docs/architecture/SEAMS-GATE2.md`` records as *pending* --
    the Style-2 declaration layer now has a Kernel/Gateway consumer.  It does not
    weaken any premise: the plan is only admissible when the gateway holds a real
    ``OFFICIAL_ORAN_DYNAMIC`` adapter, which needs a deployed definition, encoder
    and readback that do not exist hardware-free.
    """
    family = family_for_rc_style2(action_key)
    return family, official_plan(family, scope=scope, baseline=baseline, target=target)


def run_official_route(
    gateway: TokenBoundWriteGateway,
    plan: Mapping[str, Any],
    *,
    permit: Callable[[str, str, int], Any],
) -> Dict[str, Any]:
    """Drive prepare -> ready -> commit and report the three gateway results.

    ``permit(kind, expected_config_hash, sequence)`` issues the Kernel token for
    each step; the caller owns token issuance (the Kernel), this only sequences
    the gateway operations the way the vertical driver does.
    """
    baseline_hash = config_hash(plan["baselineConfig"])
    prepare = gateway.prepare(token=permit("PREPARE", baseline_hash, 0), plan=dict(plan))
    ready = gateway.ready(token=permit("READY", baseline_hash, 1))
    commit = gateway.commit(token=permit("COMMIT", baseline_hash, 2))
    return {"prepare": prepare, "ready": ready, "commit": commit}
