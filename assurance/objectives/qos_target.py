"""``QoSTarget`` — objective family module.

Owner lane: **OBJ1** (``docs/architecture/SEAMS-GATE4.md`` section 3).

Wire-kind mapping v1.2 expresses this family through the released
``AIC_UECellSteering_1.0.0`` policy: the actuator selects a cell, while the
objective remains a QoS objective because its mandatory predicates are the
O1 cell-scope ``RRU.PrbDl`` series and the delivered KPM Format 3 per-UE
``DRB.UEThpDl`` series.  This gate does not claim that steering is a PRB-quota
control.  E2SM-RC Style 2 / Action 6 belongs to the Gate 8 SliceSLATarget
actuator study and is absent from this bundle.

The registry record for this family is
``assurance.objectives.registry.record_for("QoSTarget")``.  It is the honest
statement of what this deployment can and cannot do for this objective, and a
seat below may not be filled in a way that contradicts it: changing what the
family claims means editing the record and the SEAMS document in the same
change, with the evidence that justifies it.
"""

from __future__ import annotations

from typing import Any, Mapping, Tuple

from assurance.contracts.capability import DeploymentBinding
from assurance.core.axes import TrialOutcome
from assurance.objectives.family import (
    ExpectedOutcome, ExpectedConfiguration, KpiUse,
    KpiDeclaration,
    ObjectiveContractBundle,
    ObjectiveFamilyModule,
    PolicyLifecycle,
    ScenarioName,
)
from assurance.objectives.traffic_steering import _bundle, _expectations, _oracle

__all__ = ["QoSTargetFamily"]


class QoSTargetFamily(ObjectiveFamilyModule):
    """Seats frozen by ``docs/architecture/SEAMS-GATE4.md``; bodies owned by OBJ1."""

    family = "QoSTarget"
    lane = "OBJ1"

    def contract_bundle(
        self, *, scope: Mapping[str, Any], deployment_binding: DeploymentBinding
    ) -> ObjectiveContractBundle:
        """Target/Harm/Measurement set for a quality floor over one cell scope.

        Body owned by lane OBJ1 (``docs/architecture/SEAMS-GATE4.md``).
        """
        return _bundle(family=self.family, scope=scope, deployment=deployment_binding, qos=True, steering=False)

    def candidate_parameters(self) -> Mapping[str, Tuple[str, ...]]:
        """The finite set of cells whose selection can meet the QoS predicates.

        Body owned by lane OBJ1 (``docs/architecture/SEAMS-GATE4.md``).
        """
        return {"servingCell": ("12345678", "87654321")}

    def policy_lifecycle(self) -> PolicyLifecycle:
        """R1 and A1-P lifecycle for QoS-by-cell-selection.

        Body owned by lane OBJ1 (``docs/architecture/SEAMS-GATE4.md``).
        """
        return PolicyLifecycle("AIC_UECellSteering_1.0.0", ("R1_REQUESTED", "A1_POLICY_CREATED", "E2_CONTROL_ACKNOWLEDGED", "READBACK_ENFORCED", "R1_STATUS"), "content hash plus R1 idempotency key", "one trial's O1 RRU.PrbDl and KPM Format 3 DRB.UEThpDl completed-hold evidence, with UE.ServingCell actuator readback", "The actuator is cell steering; QoS is achieved through cell selection. No new A1 policy type or Style 2 backend path is claimed in this gate.")

    def kpi_declaration(self) -> Tuple[KpiDeclaration, ...]:
        """Decision and assurance KPIs, over measurements the deployment delivers.

        Body owned by lane OBJ1 (``docs/architecture/SEAMS-GATE4.md``).
        """
        return (
            KpiDeclaration("measurement/QoSTarget/dl-prb", KpiUse.DECISION, "NRCellDU", "percent", 60000, 60000, "O1", "RRU.PrbDl from an UNLOCKED PerfMetricJob"),
            KpiDeclaration("measurement/QoSTarget/dl-prb", KpiUse.ASSURANCE, "NRCellDU", "percent", 60000, 60000, "O1", "cell-scope QoS headroom; never substituted with a per-UE counter"),
            KpiDeclaration("measurement/QoSTarget/ue-throughput", KpiUse.DECISION, "UE", "kbit/s", 60000, 60000, "E2SM-KPM 2.03 Format 3", "delivered DRB.UEThpDl per-UE counter, aggregated from the native 1000 ms stream"),
            KpiDeclaration("measurement/QoSTarget/ue-throughput", KpiUse.ASSURANCE, "UE", "kbit/s", 60000, 60000, "E2SM-KPM 2.03 Format 3", "per-UE QoS floor in the same trial"),
            KpiDeclaration("measurement/QoSTarget/serving-cell-min", KpiUse.ASSURANCE, "UE", "nci", 1000, 2000, "E2SM-KPM 2.03 Format 3", "cell-steering actuator readback on the native 1000 ms stream; not a QoS substitute"),
        )

    def terminal_oracle(self, evaluation: Mapping[str, Any]) -> TrialOutcome:
        """Name the terminal outcome for this family from the Kernel's axes.

        Body owned by lane OBJ1 (``docs/architecture/SEAMS-GATE4.md``).
        """
        return _oracle(evaluation)

    def hardware_free_expectations(self) -> Mapping[ScenarioName, ExpectedOutcome]:
        """What each verdict-bearing scenario of the shared matrix must end as.

        Body owned by lane OBJ1 (``docs/architecture/SEAMS-GATE4.md``).
        """
        return _expectations()
