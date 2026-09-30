"""QoETarget hardware-free contract over an honest episode QoE measurement."""
from __future__ import annotations
from typing import Any, Mapping, Tuple
from assurance.contracts.capability import DeploymentBinding
from assurance.core.axes import TrialOutcome
from assurance.objectives.family import ExpectedOutcome, KpiDeclaration, KpiUse, ObjectiveContractBundle, ObjectiveFamilyModule, PolicyLifecycle, ScenarioName
from assurance.objectives.traffic_steering import _bundle, _expectations, _oracle

__all__ = ["QoETargetFamily"]

class QoETargetFamily(ObjectiveFamilyModule):
    """APP.QoEScore is a reproducible episode score, not claimed as live telemetry."""
    family = "QoETarget"
    lane = "OBJ2"

    def contract_bundle(self, *, scope: Mapping[str, Any], deployment_binding: DeploymentBinding) -> ObjectiveContractBundle:
        """Build the QoE target, harm, measurement, and actuation contract."""
        return _bundle(family=self.family, scope=scope, deployment=deployment_binding, qos=False, steering=False, qoe=True)

    def candidate_parameters(self) -> Mapping[str, Tuple[str, ...]]:
        """Expose the finite serving-cell candidate space."""
        return {"servingCell": ("12345678", "87654321")}

    def policy_lifecycle(self) -> PolicyLifecycle:
        """Declare the steering lifecycle used by the QoE cell selection."""
        return PolicyLifecycle("AIC_UECellSteering_1.0.0", ("R1_REQUESTED", "A1_POLICY_CREATED", "E2_CONTROL_ACKNOWLEDGED", "READBACK_ENFORCED", "R1_STATUS"), "content hash plus R1 idempotency key", "UE.ServingCell readback plus a completed APP-QOE-EPISODE-v1 hold correlated to the same UE and trial", "QoE is achieved through cell selection (wire kind PIN_TO_CELL); no new A1 policy type is claimed.")

    def kpi_declaration(self) -> Tuple[KpiDeclaration, ...]:
        """Declare episode QoE and serving-cell readback measurements."""
        return (
            KpiDeclaration("measurement/QoETarget/application-experience", KpiUse.DECISION, "UE trial episode", "qoe-score", 1000, 2000, "APP-QOE-EPISODE-v1: oai-ext-dn DL flow and UE tun observations", "Deterministic normalized delivered-goodput, one-way-latency, jitter and loss score; KPM Format-3 identifies the UE. Hardware-free until live raw traces are wired."),
            KpiDeclaration("measurement/QoETarget/application-experience", KpiUse.ASSURANCE, "UE trial episode", "qoe-score", 1000, 2000, "APP-QOE-EPISODE-v1: oai-ext-dn DL flow and UE tun observations", "Retained component observations and formula version reproduce the score; no PRB or throughput-only proxy is accepted."),
            KpiDeclaration("measurement/QoETarget/serving-cell-min", KpiUse.ASSURANCE, "UE", "nci", 1000, 2000, "E2SM-KPM 2.03 Format 3", "actuator readback and UE attribution, not the QoE verdict"),
        )

    def terminal_oracle(self, evaluation: Mapping[str, Any]) -> TrialOutcome:
        """Reduce a completed hardware-free QoE evaluation."""
        return _oracle(evaluation)

    def hardware_free_expectations(self) -> Mapping[ScenarioName, ExpectedOutcome]:
        """Return the complete shared hardware-free scenario contract."""
        return _expectations()
