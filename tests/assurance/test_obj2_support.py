"""Shared fixture-building code for the three OBJ2 family test modules.

Not a test module in the sense ``discover`` is meant to collect assertions
from (it defines no ``TestCase``), but it is named ``test_obj2_*`` so it sits
inside the lane's granted file namespace (``docs/architecture/SEAMS-GATE4.md``
section 3) rather than adding a new, ungranted filename.  The pattern mirrors
``tests/assurance/vertical_support.py`` and
``tests/assurance/pin_to_cell_support.py``: real contract construction through
the frozen family seat, no stub of a lane's own component.

Owner lane: **OBJ2**.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from assurance.collector.mock_source import MockMeasurementSource, normal_timeseries_source, stale_source
from assurance.contracts.capability import DeploymentBinding, TransportSecurity
from assurance.core.envelopes import ASSURANCE_SCHEMA_VERSION
from assurance.gateway.mock_adapter import FaultInjection
from assurance.objectives.family import ScenarioName
from assurance.objectives.ue_level_target import (
    COUNTER_ID,
    HOME_NCI,
    NCI_UNIT,
    TARGET_NCI,
    UELevelTargetFamily,
)
from tests.assurance.objective_harness import FamilyCase

START = "2026-08-24T09:00:00.000000Z"

UE_ID = "ue-1"
CASE_ID = "case/ue-level-target"
CELL_ID = "cell/ue-level-target"
SAMPLE_SCOPE: Mapping[str, str] = {"ueId": UE_ID}

SCOPE: Mapping[str, Any] = {
    "ueId": UE_ID,
    "targetServingCell": TARGET_NCI,
    "homeServingCell": HOME_NCI,
}


def deployment_binding(contract_id: str = "deployment/ue-level-r1") -> DeploymentBinding:
    return DeploymentBinding(
        contract_id=contract_id,
        version="1.0.0",
        schema_version=ASSURANCE_SCHEMA_VERSION,
        document_status="NORMATIVE",
        standard_mapping={"a1p": "2.0"},
        endpoint_id="r1",
        base_url="https://r1.lab.invalid",
        transport_security=TransportSecurity.MTLS,
        secret_refs={"clientSecret": "env:R1_CLIENT_SECRET"},
        trust_anchor_ref="file:/etc/ssl/r1-ca.pem",
    )


def ue_level_target_bundle():
    return UELevelTargetFamily().contract_bundle(
        scope=SCOPE, deployment_binding=deployment_binding()
    )


def serving_cell_series(*, start: str, value: int, count: int = 4) -> MockMeasurementSource:
    return normal_timeseries_source(
        counter_id=COUNTER_ID,
        scope=SAMPLE_SCOPE,
        start=start,
        cadence_ms=1000,
        count=count,
        value=float(value),
        unit=NCI_UNIT,
        source_id="mock-ue-level-serving-cell",
    )


def _collector_for(scenario: ScenarioName, start: str) -> MockMeasurementSource:
    if scenario is ScenarioName.NEGATIVE:
        # A clean, sufficient measurement of the wrong cell -- the UE never
        # left home, which is a real semantic failure and not an undecidable
        # trace.
        return serving_cell_series(start=start, value=HOME_NCI)
    if scenario is ScenarioName.STALE:
        return stale_source(
            counter_id=COUNTER_ID,
            scope=SAMPLE_SCOPE,
            observed_at=start,
            cadence_ms=1000,
            value=float(TARGET_NCI),
            unit=NCI_UNIT,
            source_id="mock-ue-level-stale",
        )
    if scenario is ScenarioName.MISSING:
        return MockMeasurementSource(script=[], scope=SAMPLE_SCOPE)
    return serving_cell_series(start=start, value=TARGET_NCI)


def _faults_for(scenario: ScenarioName) -> Optional[FaultInjection]:
    if scenario is ScenarioName.PARTIAL_EFFECT:
        # The one control axis this deployment actuates through (E2SM-RC
        # Style 3 Action 1) cannot land halfway in the sense a multi-axis
        # plan can -- there is no intermediate configuration between "still
        # home" and "on the target cell".  What this deployment's real
        # lifecycle *does* separate into two steps is the A1-P policy object
        # and the RC control action it drives (``policy_lifecycle``'s
        # ``A1_POLICY_CREATED`` and ``A1_POLICY_ENFORCE_STATUS_REPORTED``):
        # the object can be created while the RC control it is supposed to
        # cause is refused at the RAN, e.g. admission control on the target
        # cell rejects the handover at the moment of execution after an
        # earlier feasibility check passed.  ``fail_axes`` on the single RC
        # axis at commit -- after prepare/ready have already validated the
        # plan cleanly -- is exactly that: the policy exists, the control it
        # was meant to cause never landed, and the gateway confirms the
        # baseline is still live rather than guessing.  See
        # ``ue_level_target.py``'s ``hardware_free_expectations`` for the
        # terminal state this actually produces, verified empirically
        # through ``ObjectiveHarness`` before being declared here.
        return FaultInjection(fail_axes={"servingCell"})
    return None


def ue_level_target_case() -> FamilyCase:
    return FamilyCase(
        bundle=ue_level_target_bundle(),
        expectations=UELevelTargetFamily().hardware_free_expectations(),
        collector_for=_collector_for,
        case_id=CASE_ID,
        evidence_cell_id=CELL_ID,
        faults_for=_faults_for,
        start=START,
    )
