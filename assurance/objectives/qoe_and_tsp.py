"""QoEandTSP: QoE and steering predicates judged in one hardware-free trial."""
from __future__ import annotations
from typing import Any, Mapping, Tuple
from assurance.contracts.capability import DeploymentBinding
from assurance.core.axes import TrialOutcome
from assurance.objectives.family import ExpectedOutcome, KpiDeclaration, KpiUse, ObjectiveContractBundle, ObjectiveFamilyModule, PolicyLifecycle, ScenarioName
from assurance.objectives.traffic_steering import _bundle, _expectations, _oracle

__all__ = ["QoEandTSPFamily"]

class QoEandTSPFamily(ObjectiveFamilyModule):
    """Judge QoE and steering predicates together in one trial."""
    family = "QoEandTSP"
    lane = "OBJ2"

    def contract_bundle(self, *, scope: Mapping[str, Any], deployment_binding: DeploymentBinding) -> ObjectiveContractBundle:
        """Build one contract carrying both component predicate sets."""
        bundle = _bundle(family=self.family, scope=scope, deployment=deployment_binding, qos=False, steering=True, qoe=True)
        return ObjectiveContractBundle(**{**bundle.__dict__, "component_predicates": {
            "QoETarget": ("application-experience-floor",),
            "TrafficSteeringPreference": ("serving-cell-preferred-min", "serving-cell-preferred-max"),
        }})

    def candidate_parameters(self) -> Mapping[str, Tuple[str, ...]]:
        """Expose the finite joint-trial serving-cell candidate space."""
        return {"servingCell": ("87654321",)}

    def policy_lifecycle(self) -> PolicyLifecycle:
        """Declare the joint steering and readback lifecycle."""
        return PolicyLifecycle("AIC_UECellSteering_1.0.0", ("R1_REQUESTED", "A1_POLICY_CREATED", "E2_CONTROL_ACKNOWLEDGED", "JOINT_READBACK_ENFORCED", "R1_STATUS"), "content hash plus R1 idempotency key", "the QoE predicate and both serving-cell predicates pass inside the same completed hold", "Composite uses PIN_TO_CELL cell selection; no composite A1 type is claimed.")

    def kpi_declaration(self) -> Tuple[KpiDeclaration, ...]:
        """Declare QoE and serving-cell evidence for the same hold."""
        return (
            KpiDeclaration("measurement/QoEandTSP/application-experience", KpiUse.ASSURANCE, "UE trial episode", "qoe-score", 1000, 2000, "APP-QOE-EPISODE-v1", "raw DL goodput/one-way latency/jitter/loss formula, correlated by KPM Format-3"),
            KpiDeclaration("measurement/QoEandTSP/serving-cell-min", KpiUse.ASSURANCE, "UE", "nci", 1000, 2000, "E2SM-KPM 2.03 Format 3", "minimum serving identity in the same hold"),
            KpiDeclaration("measurement/QoEandTSP/serving-cell-max", KpiUse.ASSURANCE, "UE", "nci", 1000, 2000, "E2SM-KPM 2.03 Format 3", "maximum serving identity in the same hold"),
        )

    def terminal_oracle(self, evaluation: Mapping[str, Any]) -> TrialOutcome:
        """Reduce a completed joint hardware-free evaluation."""
        return _oracle(evaluation)

    def hardware_free_expectations(self) -> Mapping[ScenarioName, ExpectedOutcome]:
        """Return the complete shared hardware-free scenario contract."""
        return _expectations()
