"""Digest-pinned deployment binding and objective admission.

Two things live here and nothing else:

* :mod:`oran.integration.deployment_binding` reads the composed release's
  *contract*
  documents - capability manifest, release manifest, contract digests, E2
  inventory, endpoint descriptor, status source, integration-inputs fragment -
  under their published digests.  It never reads, extracts or imports release
  production source.
* :mod:`oran.integration.objectives` turns those contracts into the objective
  advertisement the console offers and the coordinator is allowed to submit.

The separation matters: a deployment binds by *contract*, so a rebuild that
keeps its published contracts changes nothing here, and one that changes them
fails closed at load time rather than at actuation time.

The retired module name remains importable as a compatibility shim beside
:mod:`oran.integration.deployment_binding`, but this package re-exports only
the current spellings.
"""

from .deployment_binding import (
    BINDING_FILENAME, FROZEN_DEPLOYMENT_BINDING, LEGACY_BINDING_FILENAME,
    DeploymentBindingContracts,
    DeploymentBindingError, DeploymentBindingIdentity, resolve_deployment_binding,
)
from .objectives import (
    EXECUTABLE_OBJECTIVES, ExtensionAdvertisement, ObjectiveAdvertisement,
    ObjectiveNotExecutable, advertise, assert_submittable,
)

__all__ = [
    "BINDING_FILENAME", "EXECUTABLE_OBJECTIVES", "FROZEN_DEPLOYMENT_BINDING",
    "LEGACY_BINDING_FILENAME",
    "DeploymentBindingContracts", "DeploymentBindingError",
    "DeploymentBindingIdentity", "ExtensionAdvertisement",
    "ObjectiveAdvertisement", "ObjectiveNotExecutable", "advertise",
    "assert_submittable", "resolve_deployment_binding",
]
