"""SliceSLATarget hardware-free S-NSSAI measurement and quota contract."""
from __future__ import annotations
from dataclasses import replace
from typing import Any, Mapping, Tuple
from assurance.contracts.capability import ActuatorPath, DeploymentBinding
from assurance.core.axes import TrialOutcome
from assurance.objectives.family import ExpectedOutcome, KpiDeclaration, KpiUse, ObjectiveContractBundle, ObjectiveFamilyModule, PolicyLifecycle, ScenarioName
from assurance.objectives.traffic_steering import _bundle, _expectations, _oracle

__all__ = ["SliceSLATargetFamily"]

def _slice_bundle(scope: Mapping[str, Any], deployment: DeploymentBinding) -> ObjectiveContractBundle:
    """Adapt the proven three-counter bundle to slice-labelled HF contracts."""
    family = "SliceSLATarget"
    sst, sd = str(scope.get("sst", "1")), str(scope.get("sd", "000001"))
    cell = str(scope.get("cellId", "NRCellDU-1"))
    slice_entity = f"snssai-{sst}-{sd}"
    selector = {"ueId": slice_entity, "cellId": cell, "sst": sst, "sd": sd}
    base = _bundle(family=family, scope={"ueId": f"snssai-{sst}-{sd}", "cellId": cell}, deployment=deployment, qos=True, steering=False)
    counter_specs = {
        "counter/serving-cell": ("counter/slice-quota-min-readback", "RAN.SlicePrbQuotaMin", "percent"),
        "counter/rru-prb-dl": ("counter/snssai-dl-prb", "RAN.SliceDlPrbUtilisation", "percent"),
        "counter/kpm-f3-drb-ue-thp-dl": ("counter/snssai-core-throughput", "CORE.SliceSessionThroughput", "kbit/s"),
    }
    counters = tuple(replace(c, counter_id=counter_specs[c.counter_id][0], deployment_counter_name=counter_specs[c.counter_id][1], scope_keys=("ueId", "cellId", "sst", "sd"), unit=counter_specs[c.counter_id][2], native_cadence_ms=1000) for c in base.counters)
    measurement_ids = {}
    measurements = []
    for m in base.measurements:
        if "serving-cell" in m.contract_id:
            suffix = "min" if m.contract_id.endswith("min") else "max"
            new_id = f"measurement/{family}/slice-quota-{suffix}"
        elif m.contract_id.endswith("dl-prb"):
            new_id = f"measurement/{family}/snssai-dl-prb"
        else:
            new_id = f"measurement/{family}/snssai-core-throughput"
        measurement_ids[m.contract_id] = new_id
        measurements.append(replace(m, contract_id=new_id, counter_id=counter_specs[m.counter_id][0], scope_selector=selector, membership_snapshot=(slice_entity,), cadence_ms=1000, window_width_ms=3000, window_stride_ms=3000, hold_ms=3000, freshness_bound_ms=2000))
    predicate_ids = {"dl-prb-headroom": "ran-slice-prb-headroom", "ue-throughput-floor": "core-slice-throughput-floor"}
    predicates = tuple(replace(p, predicate_id=predicate_ids[p.predicate_id], constraint=replace(p.constraint, measurement_ref=measurement_ids[p.constraint.measurement_ref])) for p in base.target.predicates)
    capability_id = f"capability/{family}/slice-quota"
    actuator_id = f"actuator/{family}/slice-quota"
    actuator = replace(base.actuators[0], contract_id=actuator_id, capability_ref=capability_id, path=ActuatorPath.OFFICIAL_ORAN_DYNAMIC, policy_type_id="AIC_SliceSLATarget_1.0.0", service_model={"serviceModel": "E2SM-RC", "version": "1.03", "style": "2", "action": "6", "profile": "RRM-POLICY-RATIO-LIST"}, readback_measurement_ref=f"measurement/{family}/slice-quota-min")
    option = replace(base.target.options[0], contract_id=f"option/{family}/slice-quota", capability_ref=capability_id, parameter_space={"slicePrbQuota": ("sst=1,sd=000001,min=30,max=90,dedicated=15",)})
    target = replace(base.target, scope_selector=selector, predicates=predicates, options=(option,), hold_ms=3000)
    watchdog = replace(base.watchdogs[0], trigger=predicates[0].constraint)
    harm = replace(base.harm, scope_selector=selector, watchdogs=(watchdog,))
    capability = replace(base.capabilities[0], contract_id=capability_id, capability_id=capability_id, constraints=tuple(p.constraint for p in predicates), actuator_refs=(actuator_id,), measurement_refs=tuple(m.contract_id for m in measurements), interface_versions={"a1p": "2", "e2smRc": "1.03", "e2smKpm": "2.03", "coreSliceEvidence": "HF-v1"})
    composition = replace(base.composition, capability_refs=(capability_id,))
    return replace(base, counters=counters, measurements=tuple(measurements), target=target, watchdogs=(watchdog,), harm=harm, actuators=(actuator,), capabilities=(capability,), composition=composition, baseline_config={"slicePrbQuota": "sst=1,sd=000001,min=10,max=100,dedicated=0"}, safe_state={"slicePrbQuota": "sst=1,sd=000001,min=10,max=100,dedicated=0"}, scope=selector, sample_scope=selector)

class SliceSLATargetFamily(ObjectiveFamilyModule):
    """Judge the hardware-free S-NSSAI quota and evidence contract."""
    family = "SliceSLATarget"
    lane = "OBJ3"
    def contract_bundle(self, *, scope: Mapping[str, Any], deployment_binding: DeploymentBinding) -> ObjectiveContractBundle:
        """Build the slice-scoped target, harm, measurement, and quota contract."""
        return _slice_bundle(scope, deployment_binding)
    def candidate_parameters(self) -> Mapping[str, Tuple[str, ...]]:
        """Expose the finite slice quota candidate space."""
        return {"slicePrbQuota": ("sst=1,sd=000001,min=20,max=90,dedicated=10", "sst=1,sd=000001,min=30,max=90,dedicated=15")}
    def policy_lifecycle(self) -> PolicyLifecycle:
        """Declare the hardware-free Style 2 Action 6 lifecycle."""
        return PolicyLifecycle("AIC_SliceSLATarget_1.0.0", ("R1_POLICY_TYPE_LOOKUP", "R1_CREATE_POLICY", "A1P_POLICY_STATE_ACTIVE", "E2_RC_STYLE2_ACTION6_ACKNOWLEDGED", "SLICE_READBACK_ENFORCED", "R1_DELETE_POLICY"), "policy content hash and Kernel permit idempotency key", "quota readback plus fresh RAN and Core samples carrying the same S-NSSAI; an ACK alone is not enforcement", "Hardware-free only; policy type is not advertised by the deployed composition.")
    def kpi_declaration(self) -> Tuple[KpiDeclaration, ...]:
        """Declare slice-labelled RAN, Core, and quota measurements."""
        return (
        KpiDeclaration("measurement/SliceSLATarget/snssai-dl-prb", KpiUse.ASSURANCE, "S-NSSAI within NRCellDU", "percent", 1000, 2000, "HF S-NSSAI-labelled RAN collector", "live TS 28.552/KPM slice labelling remains unwired"),
        KpiDeclaration("measurement/SliceSLATarget/snssai-core-throughput", KpiUse.ASSURANCE, "S-NSSAI at Core session anchor", "kbit/s", 1000, 2000, "HF S-NSSAI-labelled Core collector", "live Core evidence remains unwired"),
        KpiDeclaration("measurement/SliceSLATarget/slice-quota-min", KpiUse.DECISION, "S-NSSAI within NRCellDU", "percent", 1000, 2000, "E2SM-RC 1.03 Style 2 / Action 6 readback", "nested RRM Policy Ratio List hardware-free readback"),)
    def terminal_oracle(self, evaluation: Mapping[str, Any]) -> TrialOutcome:
        """Reduce a completed hardware-free slice evaluation."""
        return _oracle(evaluation)
    def hardware_free_expectations(self) -> Mapping[ScenarioName, ExpectedOutcome]:
        """Return the complete shared hardware-free scenario contract."""
        return _expectations()
