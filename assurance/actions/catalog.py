"""Enumerable, hardware-free control-action contracts for the xApp proposer.

This module is declaration-only.  It never opens a transport.  ``live_capable``
means the checked-in OAI build has a reversible primitive; it does not mean the
official A1/E2 path is wired or that OTA evidence exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Tuple

from assurance.contracts.capability import (
    ActuatorBinding, ActuatorDeploymentState, ActuatorParameter, ActuatorPath,
    CapabilityManifest, DeploymentBinding,
)
from assurance.contracts.harm import (
    CertifiedHarmBound, HarmContract, HarmKind, WatchdogAction, WatchdogContract,
)
from assurance.contracts.measurement import (
    Aggregation, ClockRequirement, CounterBinding, Estimator, GapPolicy,
    MeasurementContract, MeasurementSource, OverlapPolicy, UncertaintyRule,
)
from assurance.contracts.target import ComparisonOperator, TypedConstraint
from assurance.core.envelopes import ASSURANCE_SCHEMA_VERSION
from assurance.core.provenance import Provenance, TypedQuantity

__all__ = ["ActionContract", "ActionParameterError", "UE_DL_PRB_CAP_APPLY_RANGE",
           "UE_DL_PRB_CAP_UNCAPPED_SENTINEL", "action_catalog",
           "action_catalog_view", "validate_action_parameters"]

#: The frozen APPLY range for ``ue-dl-prb-cap``.  The deployed radio is exactly
#: 24 PRB and the OAI scheduler issues no new-data grant below five, so an
#: *applied* cap lives in ``[5,24]``
#: (``docs/architecture/A1-ACTION102-CONTRACT.md`` section 2.1).
#: v5.1 (2026-09-26): the radio has been 38 PRB since 09-03; ``AIC_CAP_APPLY_MAX`` widens the
#: upper end for the v5.1 free-range design only, so v47 sittings keep their contract hashes.
UE_DL_PRB_CAP_APPLY_RANGE = (5, int(__import__("os").environ.get("AIC_CAP_APPLY_MAX") or 24)
                             if "1" in (__import__("os").environ.get("AIC_POLICY_RANGE"), __import__("os").environ.get("AIC_V51"))
                             else 24)

#: ``0`` is the gNB's uncapped sentinel.  It is a legitimate observed baseline
#: and a legitimate restore value -- reverse rollback puts the captured prior
#: configuration back, uncapped included -- and it is never an APPLY candidate
#: in the ``AIC_UeDlPrbCap_2.0.0`` policy type, whose schema starts at 5.
UE_DL_PRB_CAP_UNCAPPED_SENTINEL = 0


class ActionParameterError(ValueError):
    """A proposal is outside the actuator's frozen parameter space."""


@dataclass(frozen=True)
class ActionContract:
    action_id: str
    tier: str
    binding: ActuatorBinding
    capability: CapabilityManifest
    counter: CounterBinding
    readback: MeasurementContract
    watchdog: WatchdogContract
    harm: HarmContract
    hardware_free_expectations: Mapping[str, str]


def _identity(contract_id: str, *, rc: bool = True) -> Dict[str, Any]:
    mapping = {"e2sm-rc": "1.03"} if rc else {"project-custom-control": "1.0"}
    return dict(contract_id=contract_id, version="1.0.0",
                schema_version=ASSURANCE_SCHEMA_VERSION,
                document_status="NORMATIVE", standard_mapping=mapping)


def _q(value: float, unit: str, source: str) -> TypedQuantity:
    return TypedQuantity(value, unit, Provenance.EXPERIMENT_CONFIG, source)


def _entry(*, key: str, tier: str, style: str, action: str,
           operation: str, parameters: Tuple[ActuatorParameter, ...],
           readback_name: str, readback_unit: str, deployment: DeploymentBinding,
           live_backend: str | None = None, blocker: str | None = None,
           standard_rc: bool = True, note: str = "",
           ran_parameter_path: str | None = None,
           policy_type_id: str = "ACTION_SPACE_PROPOSAL_V1",
           adapter: str | None = None) -> ActionContract:
    aid = f"actuator/action-space/{key}"
    cid = f"capability/action-space/{key}"
    mid = f"measurement/action-space/{key}/readback"
    counter_id = f"counter/action-space/{key}/readback"
    selector = {"scope": parameters[0].scope}
    counter = CounterBinding(counter_id, readback_name,
        MeasurementSource.CONFIGURATION_READBACK, tuple(selector), readback_unit,
        1000, deployment.contract_id)
    readback = MeasurementContract(
        **_identity(mid, rc=standard_rc), counter_id=counter_id,
        scope_selector=selector, membership_snapshot=(key,), cadence_ms=1000,
        window_width_ms=3000, window_stride_ms=3000,
        overlap=OverlapPolicy.DISJOINT, aggregation=Aggregation.MAX,
        estimator=Estimator.EMPIRICAL_QUANTILE, minimum_entity_count=1,
        hold_ms=3000, gap_policy=GapPolicy.REJECT_WINDOW,
        missing_interval_charge=_q(1, readback_unit, f"action-space/{key}/missing"),
        freshness_bound_ms=2000,
        clock_requirement=ClockRequirement.SYNCHRONISED_REQUIRED,
        uncertainty_rule=UncertaintyRule("configuration-exact", _q(0, readback_unit,
                                                f"action-space/{key}/readback")))
    # -1 is the configuration adapter's explicit unavailable/fault sentinel;
    # ordinary configuration values, including zero, must not fire the guard.
    trigger = TypedConstraint(mid, ComparisonOperator.EQUAL,
                              _q(-1, readback_unit, f"action-space/{key}/fault-sentinel"))
    watchdog = WatchdogContract(**_identity(f"watchdog/action-space/{key}", rc=standard_rc),
        watchdog_id=f"wd/action-space/{key}", trigger=trigger,
        action=WatchdogAction.STOP_AND_ROLLBACK, max_evaluation_latency_ms=1000)
    bound = CertifiedHarmBound(f"bound/action-space/{key}",
        _q(1000, "ms", f"action-space/{key}/calibration"),
        _q(1000, "ms", f"action-space/{key}/margin"),
        _q(2000, "ms", f"action-space/{key}/admission"),
        f"{mid}#uncertainty", selector, 2000,
        ("hardware-free/action-space/fault-injection",),
        f"proof/deterministic-timeout/{key}")
    harm = HarmContract(**_identity(f"harm/action-space/{key}", rc=standard_rc),
        harm_kind=HarmKind.TRIAL_INDUCED, scope_selector=selector,
        reserve=_q(2000, "ms", f"action-space/{key}/reserve"), bounds=(bound,),
        watchdogs=(watchdog,), missing_interval_charge=_q(1000, "ms",
                                                           f"action-space/{key}/missing"))
    service_model = {"serviceModel": "E2SM-RC" if standard_rc else "PROJECT-CUSTOM",
                     "version": "1.03" if standard_rc else "1.0",
                     "style": style, "action": action, "operation": operation}
    if ran_parameter_path is not None:
        # The nested RAN-parameter path is part of the capability gate: a style
        # and action number without it is a number, not a definition
        # (docs/architecture/A1-ACTION102-CONTRACT.md section 1).
        service_model["ranParameterPath"] = ran_parameter_path
    if adapter is not None:
        # The Write Gateway adapter permanently bound to this action.  Frozen
        # here so nothing downstream can select an adapter from proposal text.
        service_model["adapter"] = adapter
    binding = ActuatorBinding(**_identity(aid, rc=standard_rc), capability_ref=cid,
        path=ActuatorPath.OFFICIAL_ORAN_DYNAMIC, policy_type_id=policy_type_id,
        service_model=service_model, readback_measurement_ref=mid,
        deployment_binding_ref=deployment.contract_id, parameters=parameters,
        deployment_state=ActuatorDeploymentState.HARDWARE_FREE_VERIFIED,
        live_capable=live_backend is not None, live_backend=live_backend,
        live_blocking_premise=blocker, provenance_note=note)
    capability = CapabilityManifest(**_identity(cid, rc=standard_rc), capability_id=cid,
        supported_objectives=("ComposableRANAction",), constraints=(trigger,),
        actuator_refs=(aid,), measurement_refs=(mid,),
        interface_versions={"e2smRc": "1.03" if standard_rc else "NOT-AN-E2SM-RC-ACTION",
                            "contractEvidence": "HARDWARE_FREE_ROUND_TRIP"})
    return ActionContract(key, tier, binding, capability, counter, readback,
                          watchdog, harm,
                          {"positive": "APPLY_READBACK_COMMIT",
                           "negative": "PARAMETER_REFUSED_NO_EFFECT",
                           "fault": "STOP_ROLLBACK_READBACK_BASELINE"})


def action_catalog(deployment: DeploymentBinding) -> Tuple[ActionContract, ...]:
    """Return the canonical ordered action space; Tier A is considered first."""
    P = ActuatorParameter
    knob = "oai_patches/d2_actionspace_runtime_knobs.patch: telnet ci "
    no_path = "NOT_ACTUATED_BY_DEPLOYMENT: no A1/E2 encoder and OAI handler exists"
    return (
        _entry(key="cell-steering", tier="A", style="3", action="1",
            operation="UE-level cell mobility", parameters=(P("targetPrimaryCellId", "integer", "UE", "NCI"),),
            readback_name="UE.ServingCell", readback_unit="NCI", deployment=deployment,
            live_backend="existing A1/E2SM-RC handover path",
            note="Existing TrafficSteeringPreference contract; OTA evidence is owned by that objective."),
        _entry(key="slice-prb-quota", tier="A", style="2", action="6",
            operation="RRM Policy Ratio List", parameters=(
                P("sst", "integer", "S-NSSAI"), P("sd", "hex-string", "S-NSSAI"),
                P("minPrbPolicyRatio", "integer", "S-NSSAI", "percent", 0, 100),
                P("maxPrbPolicyRatio", "integer", "S-NSSAI", "percent", 0, 100),
                P("dedicatedPrbPolicyRatio", "integer", "S-NSSAI", "percent", 0, 100)),
            readback_name="RAN.SlicePrbQuotaMin", readback_unit="percent", deployment=deployment,
            live_backend="oai_patches/e2sm_rc_style2_action6_slice_prb.patch",
            note="Hardware primitive exists; deployed policy/measurement wiring remains pending."),
        # SUPPLEMENTARY, never a candidate-selecting PRIMARY action.  The exact
        # Style 2 / Action 102 / 211-212 definition, the ``[5,24] PRB`` APPLY
        # range on a 24-PRB radio, and the ``r1-cap`` adapter are all frozen by
        # ``docs/architecture/A1-ACTION102-CONTRACT.md``.  ``rnti`` stays a
        # parameter of the *executor* contract -- the xApp coordination layer
        # resolves and carries one -- but it is never an A1 policy target: the
        # Action-102 policy body carries no RNTI at all, and the released xApp
        # resolves the current one from a fresh KPM attribution
        # (``oran/action102/builders.py``).
        _entry(key="ue-dl-prb-cap", tier="A", style="2", action="102",
            ran_parameter_path="211/212", adapter="r1-cap",
            policy_type_id="AIC_UeDlPrbCap_1.0.0",
            operation="UE radio-resource-allocation ceiling", parameters=(
                P("rnti", "hex-integer", "UE",
                  description="resolved by the executor from fresh attribution; "
                              "never carried on the A1 policy"),
                P("maxDlPrbs", "integer", "UE", "PRB",
                  UE_DL_PRB_CAP_UNCAPPED_SENTINEL, UE_DL_PRB_CAP_APPLY_RANGE[1],
                  description="0 is the uncapped sentinel -- a baseline and a "
                              "restore value, never an applied candidate; an "
                              "applied cap is 5..24 PRB on this 24-PRB radio")),
            readback_name="RAN.UE.DlPrbCap",
            readback_unit="PRB", deployment=deployment,
            live_backend=knob + "prbcap",
            note="Style 2 Action 102 with RAN parameters 211 (STRUCTURE) / 212 (maxDlPrbs), as gNB1 advertises them. The official path is A1 AIC_UeDlPrbCap_1.0.0 -> the in-repo Campaign 5 producer -> our_rc_xapp -> FlexRIC -> E2SM-RC Style 2 Action 102, with RAN.UE.DlPrbCap as the configuration readback; live_backend still names the checked-in reversible telnet primitive because the live worker behind the producer is a separate lane. The cap is SUPPLEMENTARY to a PRIMARY steering action and never runs standalone."),
        _entry(key="scheduler-priority", tier="A", style="2", action="definition-dependent",
            operation="UE scheduling control", parameters=(P("rnti", "hex-integer", "UE"),
                P("pfWeight", "number", "UE", "ratio", 0.001, 100)),
            readback_name="RAN.UE.PfWeight", readback_unit="ratio", deployment=deployment,
            live_backend=knob + "sched_prio",
            note="Style 2 scheduling control; action id depends on the advertised RAN-function definition."),
        _entry(key="dl-rf-attenuation", tier="A", style="CUSTOM", action="O1-adjacent",
            operation="OAI transmit attenuation", parameters=(P("txAttenuationDb", "number", "NRCellDU", "dB"),),
            readback_name="L1M.SS-RSRP", readback_unit="dBm", deployment=deployment,
            live_backend=knob + "rfatt", standard_rc=False,
            note="Explicitly not a standard E2SM-RC action; lab L1 control with O1-adjacent semantics."),
        _entry(key="dl-mcs-bounds", tier="A", style="2", action="definition-dependent",
            operation="link-adaptation constraint", parameters=(
                P("maxDlMcs", "integer", "NRCellDU", "MCS-index", 0, 28),
                P("minDlMcs", "integer", "NRCellDU", "MCS-index", 0, 28)),
            readback_name="RAN.Cell.DlMcsBounds", readback_unit="MCS-index", deployment=deployment,
            live_backend=knob + "mcs",
            note="Style 2 control classification; numeric action id is definition-dependent."),
        _entry(key="drb-qos", tier="B", style="1", action="definition-dependent",
            operation="QoS flow to DRB mapping / DRB QoS configuration", parameters=(
                P("qfi", "integer", "UE/QoS-flow", minimum=0, maximum=63),
                P("drbId", "integer", "UE/DRB", minimum=1, maximum=32),
                P("fiveQi", "integer", "QoS-flow")), readback_name="RAN.UE.QosFlowDrbMapping",
            readback_unit="mapping", deployment=deployment, blocker=no_path),
        _entry(key="radio-access-control", tier="B", style="4", action="definition-dependent",
            operation="radio access control", parameters=(P("accessOperation", "enum", "UE/cell",
                allowed_values=("CONNECTION_RELEASE", "ACCESS_BARRING", "ADMISSION_CONTROL")),),
            readback_name="RAN.RadioAccessState", readback_unit="state", deployment=deployment, blocker=no_path),
        _entry(key="dual-connectivity", tier="B", style="5", action="definition-dependent",
            operation="SCG add/modify/release", parameters=(P("scgOperation", "enum", "UE",
                allowed_values=("ADD", "MODIFY", "RELEASE")), P("secondaryCellId", "integer", "UE", "NCI")),
            readback_name="RAN.UE.ScgState", readback_unit="state", deployment=deployment, blocker=no_path),
        _entry(key="carrier-aggregation", tier="B", style="6", action="definition-dependent",
            operation="SCell add/modify/release", parameters=(P("sCellOperation", "enum", "UE",
                allowed_values=("ADD", "MODIFY", "RELEASE")), P("sCellId", "integer", "UE", "NCI")),
            readback_name="RAN.UE.SCellState", readback_unit="state", deployment=deployment, blocker=no_path),
        _entry(key="idle-mode-mobility", tier="B", style="7", action="definition-dependent",
            operation="idle-mode cell reselection priority", parameters=(P("cellReselectionPriority", "integer", "frequency", minimum=0, maximum=7),
                P("frequency", "integer", "frequency", "ARFCN")),
            readback_name="RAN.Idle.ReselectionPriority", readback_unit="priority", deployment=deployment, blocker=no_path),
    )


def action_catalog_view(deployment: DeploymentBinding) -> Tuple[Mapping[str, Any], ...]:
    """Stable, JSON-ready view given to advisory proposers and the GUI."""
    return tuple({
        "actionId": item.action_id, "tier": item.tier,
        "serviceModel": dict(item.binding.service_model),
        "parameters": tuple({"name": p.name, "type": p.value_type, "scope": p.scope,
                             "unit": p.unit, "minimum": p.minimum, "maximum": p.maximum,
                             "allowedValues": p.allowed_values} for p in item.binding.parameters),
        "liveCapability": "LIVE_CAPABLE_UNVERIFIED" if item.binding.live_capable else "CONTRACT_ONLY",
        "hardwareFreeState": item.binding.deployment_state.value,
        "liveBackend": item.binding.live_backend,
        "liveBlockingPremise": item.binding.live_blocking_premise,
        "readbackMeasurementRef": item.binding.readback_measurement_ref,
        "watchdogRef": item.watchdog.contract_id,
        "harmContractRef": item.harm.contract_id,
        "provenanceNote": item.binding.provenance_note,
    } for item in action_catalog(deployment))


def validate_action_parameters(action: ActionContract,
                               values: Mapping[str, Any]) -> None:
    """Fail closed before a proposed action can reach Kernel admission."""
    definitions = {parameter.name: parameter for parameter in action.binding.parameters}
    if set(values) != set(definitions):
        raise ActionParameterError(
            f"{action.action_id}: expected parameters {sorted(definitions)}, got {sorted(values)}")
    for name, parameter in definitions.items():
        value = values[name]
        if parameter.value_type in {"integer", "hex-integer"}:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ActionParameterError(f"{action.action_id}.{name}: integer required")
        elif parameter.value_type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ActionParameterError(f"{action.action_id}.{name}: number required")
        elif parameter.value_type in {"enum", "hex-string"} and not isinstance(value, str):
            raise ActionParameterError(f"{action.action_id}.{name}: string required")
        if parameter.allowed_values and value not in parameter.allowed_values:
            raise ActionParameterError(f"{action.action_id}.{name}: value is not allowed")
        if parameter.minimum is not None and value < parameter.minimum:
            raise ActionParameterError(f"{action.action_id}.{name}: below minimum")
        if parameter.maximum is not None and value > parameter.maximum:
            raise ActionParameterError(f"{action.action_id}.{name}: above maximum")
    if action.action_id == "slice-prb-quota":
        if not values["dedicatedPrbPolicyRatio"] <= values["minPrbPolicyRatio"] <= values["maxPrbPolicyRatio"]:
            raise ActionParameterError("slice-prb-quota: require dedicated <= min <= max")
    if action.action_id == "dl-mcs-bounds" and values["minDlMcs"] > values["maxDlMcs"]:
        raise ActionParameterError("dl-mcs-bounds: minDlMcs exceeds maxDlMcs")
    if action.action_id == "ue-dl-prb-cap":
        cap = values["maxDlPrbs"]
        low, high = UE_DL_PRB_CAP_APPLY_RANGE
        if cap != UE_DL_PRB_CAP_UNCAPPED_SENTINEL and not low <= cap <= high:
            raise ActionParameterError(
                f"ue-dl-prb-cap: an applied cap is {low}..{high} PRB on this "
                f"{high}-PRB deployment; {UE_DL_PRB_CAP_UNCAPPED_SENTINEL} is the "
                "uncapped sentinel and every other value is refused before any write")
