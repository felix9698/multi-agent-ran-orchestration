"""Hardware-free declarations for the E2SM-RC Style 2 action patch.

This module declares actuator/readback contracts on the official O-RAN path.
It is not Kernel or Write Gateway wiring; that consumer seam is a later step.
The action catalog remains the sole authority for live-capability, backend,
blocking-premise, parameter-space, scope, and readback tags.  In particular,
deriving these declarations never promotes or downgrades those live fields.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping, Tuple

from assurance.actions.catalog import ActionContract, action_catalog, validate_action_parameters
from assurance.contracts.capability import ActuatorBinding, DeploymentBinding

__all__ = [
    "RcStyle2Action", "rc_style2_actions", "validate_rc_style2_parameters",
]


_PATCH = "oai_patches/e2sm_rc_style2_ue_actions.patch"
_PREMISES = (
    "NOT_REBUILT_INTO_DEPLOYED_BINARY",
    "XAPP_PRODUCER_ENCODER_PENDING",
    "READBACK_PRODUCER_WIRING_PENDING",
    "KERNEL_GATEWAY_WIRING_PENDING",
    "NO_OTA_EVIDENCE",
)
_ACTION_METADATA = {
    "ue-dl-prb-cap": ("UE", 102, (211, 212)),
    "scheduler-priority": ("UE", 103, (221, 222)),
    "dl-mcs-bounds": ("NRCellDU", 101, (201, 202, 203)),
}


@dataclass(frozen=True)
class RcStyle2Action:
    """One patch binding plus its catalog-owned source contract."""

    key: str
    binding: ActuatorBinding
    source: ActionContract
    scope: str
    disposition: str
    patch_text: str
    deployment_local_action_id: int
    deployment_local_parameter_ids: Tuple[int, ...]
    premises: Tuple[str, ...]


def rc_style2_actions(deployment: DeploymentBinding) -> Tuple[RcStyle2Action, ...]:
    """Declare the three patch actions without altering catalog live tags."""
    source_by_key = {item.action_id: item for item in action_catalog(deployment)}
    actions = []
    for key, (scope, action_id, parameter_ids) in _ACTION_METADATA.items():
        source = source_by_key[key]
        service_model = dict(source.binding.service_model)
        service_model.update({
            "serviceModel": "E2SM-RC",
            "version": "1.03",
            "style": "2",
            "controlHeaderFormat": "1",
            "controlMessageFormat": "1",
            "actionIdDisposition": "definition-dependent/deployment-local",
            "deploymentLocalActionId": str(action_id),
            "deploymentLocalParameterIds": ",".join(str(item) for item in parameter_ids),
        })
        disposition = (
            f"PATCH_TEXT_PROPOSED: deployment-local Action {action_id}; "
            "producer must discover the advertised RAN-function definition."
        )
        # Deliberately leave live_capable, live_backend, and
        # live_blocking_premise untouched.  The source catalog owns them.
        binding = replace(
            source.binding,
            service_model=service_model,
            provenance_note=(
                f"{source.binding.provenance_note} Official-path patch declaration only; "
                f"{disposition}"
            ),
        )
        actions.append(RcStyle2Action(
            key=key,
            binding=binding,
            source=source,
            scope=scope,
            disposition=disposition,
            patch_text=_PATCH,
            deployment_local_action_id=action_id,
            deployment_local_parameter_ids=parameter_ids,
            premises=_PREMISES,
        ))
    return tuple(actions)


def validate_rc_style2_parameters(
    action: RcStyle2Action, values: Mapping[str, object]
) -> None:
    """Use the catalog's frozen parameter constraints; add no parallel ranges."""
    validate_action_parameters(action.source, values)
