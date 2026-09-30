"""``QoSandTSP`` — objective family module.

Owner lane: **OBJ1** (``docs/architecture/SEAMS-GATE4.md`` section 3).

A composite family, and the composition rule is the whole point.  Task
section 8 forbids assembling a combined result out of a QoS pass from one
trial and a steering pass from another: both component predicate sets are
mandatory inside the same trial, over the same hold and the same validity
region.

The shared harness enforces that structurally -- it asserts every
component's mandatory predicate ids were judged inside one trial id -- so
the bundle this module returns must carry both sets on a single
``TargetContract`` rather than two contracts run in sequence.

The registry record for this family is
``assurance.objectives.registry.record_for("QoSandTSP")``.  It is the honest
statement of what this deployment can and cannot do for this objective, and a
seat below may not be filled in a way that contradicts it: changing what the
family claims means editing the record and the SEAMS document in the same
change, with the evidence that justifies it.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping, Tuple

from assurance.contracts.capability import DeploymentBinding
from assurance.core.axes import TrialOutcome
from assurance.objectives.family import (
    ExpectedOutcome, KpiUse,
    KpiDeclaration,
    ObjectiveContractBundle,
    ObjectiveFamilyModule,
    PolicyLifecycle,
    ScenarioName,
)
from assurance.objectives.traffic_steering import _bundle, _expectations, _oracle

__all__ = ["QoSandTSPFamily"]


class QoSandTSPFamily(ObjectiveFamilyModule):
    """Seats frozen by ``docs/architecture/SEAMS-GATE4.md``; bodies owned by OBJ1."""

    family = "QoSandTSP"
    lane = "OBJ1"

    def contract_bundle(
        self, *, scope: Mapping[str, Any], deployment_binding: DeploymentBinding
    ) -> ObjectiveContractBundle:
        """One target contract carrying both component predicate sets as mandatory, so a single trial judges the combination.

        Body owned by lane OBJ1 (``docs/architecture/SEAMS-GATE4.md``).
        """
        bundle = _bundle(family=self.family, scope=scope, deployment=deployment_binding, qos=True, steering=True)
        return replace(bundle, component_predicates={
            "QoSTarget": ("dl-prb-headroom", "ue-throughput-floor"),
            "TrafficSteeringPreference": (
                "serving-cell-preferred-min", "serving-cell-preferred-max",
            ),
        })

    def candidate_parameters(self) -> Mapping[str, Tuple[str, ...]]:
        """The finite cell-selection space shared by both component objectives.

        Body owned by lane OBJ1 (``docs/architecture/SEAMS-GATE4.md``).
        """
        return {"servingCell": ("12345678", "87654321")}

    def policy_lifecycle(self) -> PolicyLifecycle:
        """R1 and A1-P lifecycle for one combined steering submission.

        Body owned by lane OBJ1 (``docs/architecture/SEAMS-GATE4.md``).
        """
        return PolicyLifecycle("AIC_UECellSteering_1.0.0", ("R1_REQUESTED", "A1_POLICY_CREATED", "E2_CONTROL_ACKNOWLEDGED", "READBACK_ENFORCED", "R1_STATUS"), "joint content hash plus R1 idempotency key", "one trial's O1 RRU.PrbDl, KPM Format 3 DRB.UEThpDl and UE.ServingCell completed-hold evidence", "The actuator is cell steering; QoS is achieved through cell selection. Both QoS and TSP predicate sets are mandatory in the same trial.")

    def kpi_declaration(self) -> Tuple[KpiDeclaration, ...]:
        """Decision and assurance KPIs for both components, distinctly scoped.

        Body owned by lane OBJ1 (``docs/architecture/SEAMS-GATE4.md``).
        """
        return (
            KpiDeclaration("measurement/QoSandTSP/dl-prb", KpiUse.DECISION, "NRCellDU", "percent", 60000, 60000, "O1", "RRU.PrbDl from an UNLOCKED PerfMetricJob"),
            KpiDeclaration("measurement/QoSandTSP/dl-prb", KpiUse.ASSURANCE, "NRCellDU", "percent", 60000, 60000, "O1", "QoS component cell-scope predicate"),
            KpiDeclaration("measurement/QoSandTSP/ue-throughput", KpiUse.DECISION, "UE", "kbit/s", 60000, 60000, "E2SM-KPM 2.03 Format 3", "delivered DRB.UEThpDl per-UE counter, aggregated from the native 1000 ms stream"),
            KpiDeclaration("measurement/QoSandTSP/ue-throughput", KpiUse.ASSURANCE, "UE", "kbit/s", 60000, 60000, "E2SM-KPM 2.03 Format 3", "QoS component per-UE floor"),
            KpiDeclaration("measurement/QoSandTSP/serving-cell-min", KpiUse.ASSURANCE, "UE", "nci", 1000, 2000, "E2SM-KPM 2.03 Format 3", "steering component minimum on the native 1000 ms stream"),
            KpiDeclaration("measurement/QoSandTSP/serving-cell-max", KpiUse.ASSURANCE, "UE", "nci", 1000, 2000, "E2SM-KPM 2.03 Format 3", "steering component maximum on the native 1000 ms stream"),
        )

    def terminal_oracle(self, evaluation: Mapping[str, Any]) -> TrialOutcome:
        """Name the terminal outcome.  A combined success requires every component's mandatory predicates to pass in this one trial.

        Body owned by lane OBJ1 (``docs/architecture/SEAMS-GATE4.md``).
        """
        return _oracle(evaluation)

    def hardware_free_expectations(self) -> Mapping[ScenarioName, ExpectedOutcome]:
        """What each verdict-bearing scenario of the shared matrix must end as.

        Body owned by lane OBJ1 (``docs/architecture/SEAMS-GATE4.md``).
        """
        return _expectations()
