"""Compatibility imports for the retired deployment-binding module name.

New code imports :mod:`oran.integration.deployment_binding`.  These aliases
preserve existing callers while the composed-release digest and allowlist
verification remain implemented in exactly one module.
"""

from .deployment_binding import (
    BINDING_FILENAME,
    CONTRACT_FILES,
    FORBIDDEN_PREFIXES,
    FROZEN_DEPLOYMENT_BINDING,
    LEGACY_BINDING_FILENAME,
    DeploymentBindingContracts,
    DeploymentBindingError,
    DeploymentBindingIdentity,
    resolve_deployment_binding,
)
from .deployment_binding import _read

FROZEN_INTEGRATION_BINDING = FROZEN_DEPLOYMENT_BINDING
LowerReleaseContracts = DeploymentBindingContracts
LowerReleaseError = DeploymentBindingError
def LowerReleaseIdentity(*args, **kwargs):
    """Build the modern identity from the legacy constructor spelling."""
    if "upper_release" in kwargs:
        kwargs["composed_component_release"] = kwargs.pop("upper_release")
    return DeploymentBindingIdentity(*args, **kwargs)
resolve_binding = resolve_deployment_binding

__all__ = [
    "BINDING_FILENAME", "CONTRACT_FILES", "FORBIDDEN_PREFIXES",
    "LEGACY_BINDING_FILENAME",
    "FROZEN_INTEGRATION_BINDING", "LowerReleaseContracts",
    "LowerReleaseError", "LowerReleaseIdentity", "resolve_binding",
]
