"""Capability Manifest, Composition Manifest and the three bindings.

Owner lane: **KCON**.

Design section 6.2 requires "Capability Manifest and Composition Manifest with
typed constraints" and "Actuator Binding, Counter Binding and secret-free
Deployment Binding".  The split is the point:

* a **capability** is what an xApp or a policy type can do, expressed in
  contract terms and constrained by typed constraints;
* a **composition** is which capabilities are deployed together and what that
  combination additionally forbids;
* a **binding** is how that capability reaches this particular testbed.

Design section 9 fixes the only official dynamic path -- Kernel to Write
Gateway to R1 to Non-RT RIC to A1-P to xApp to FlexRIC to E2SM to OAI gNB --
and forbids replacing it with direct gNB control.  :class:`ActuatorPath` is
that constraint written as data, and :class:`ActuatorBinding` refuses to name
anything else.

`secret_refs` is the whole security surface here.  Task section 4.6 and design
section 17.10 forbid actual credentials, passwords, tokens and private keys in
source, manifests, archives, logs, GUI and export; a deployment binding
therefore stores *references* that a runtime resolves, and
``validation.assert_secret_free`` is the check that keeps it that way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Optional, Tuple

from assurance.contracts.common import ContractIdentity, frozen_mapping, frozen_tuple
from assurance.contracts.target import TypedConstraint

__all__ = [
    "ActuatorDeploymentState",
    "ActuatorBinding",
    "ActuatorParameter",
    "ActuatorPath",
    "CapabilityManifest",
    "CompositionManifest",
    "DeploymentBinding",
    "TransportSecurity",
]


class ActuatorDeploymentState(Enum):
    """Evidence-backed availability of an actuator in this deployment."""

    UNSPECIFIED = "UNSPECIFIED"
    LIVE_CAPABLE_UNVERIFIED = "LIVE_CAPABLE_UNVERIFIED"
    HARDWARE_FREE_VERIFIED = "HARDWARE_FREE_VERIFIED"
    OTA_LIVE_VERIFIED = "OTA_LIVE_VERIFIED"


@dataclass(frozen=True)
class ActuatorParameter:
    """One typed, bounded control parameter exposed to a proposer."""

    name: str
    value_type: str
    scope: str
    unit: str = "1"
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    allowed_values: Tuple[str, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_values", frozen_tuple(self.allowed_values))


class ActuatorPath(Enum):
    """The permitted actuation paths.

    Exactly two, and they are not interchangeable.

    ``OFFICIAL_ORAN_DYNAMIC`` is the section 9 chain and the only path whose
    effects count as objective effects or OTA evidence.

    ``LAB_SETUP_PREPARATION`` covers SSH, Telnet, OAI CLI, process control and
    USRP power.  Design section 9 and task section 7.5: those actions "must not
    be catalog candidates and must not be counted as an objective effect".
    Naming the path in the binding means a candidate generator can refuse to
    admit a Lab Setup capability structurally, instead of relying on nobody
    adding one.
    """

    OFFICIAL_ORAN_DYNAMIC = "OFFICIAL_ORAN_DYNAMIC"
    LAB_SETUP_PREPARATION = "LAB_SETUP_PREPARATION"


class TransportSecurity(Enum):
    """Endpoint security the deployment actually uses (design section 5)."""

    MTLS = "MTLS"
    OAUTH2 = "OAUTH2"
    MTLS_AND_OAUTH2 = "MTLS_AND_OAUTH2"
    #: Only legitimate for a loopback lab endpoint; recorded so the Cockpit
    #: can show it rather than letting it pass unnoticed.
    NONE = "NONE"


@dataclass(frozen=True)
class ActuatorBinding(ContractIdentity):
    """How one capability's effect is actually requested.

    Attributes
    ----------
    capability_ref:
        Capability manifest this binds.
    path:
        :class:`ActuatorPath`.  Only ``OFFICIAL_ORAN_DYNAMIC`` may appear on a
        catalog candidate.
    policy_type_id:
        A1-P policy type identifier the Write Gateway will create.
    service_model:
        E2 service model, style, action and parameter mapping identifiers,
        e.g. ``{"serviceModel": "E2SM-RC", "style": "3", "action": "..."}``
        (task section 8.7).
    supports_idempotent_create:
        Whether a repeated create with the same content is safe.  Design
        section 4.4 requires idempotency keys; a binding that cannot honour
        them needs the Kernel to fence instead.
    rollback_supported:
        Whether the effect can be reversed through the same path.  A capability
        without a rollback path fails closed and is not advertised
        (design section 10).
    readback_measurement_ref:
        Measurement contract that reads the applied configuration back.
        Design section 9: an A1 create or an E2 ACK is not success; the
        contracted actual effect must be read back.
    deployment_binding_ref:
        Which deployment binding reaches this actuator.
    """

    capability_ref: str
    path: ActuatorPath
    policy_type_id: str
    service_model: Mapping[str, str]
    readback_measurement_ref: str
    deployment_binding_ref: str
    supports_idempotent_create: bool = True
    rollback_supported: bool = True
    parameters: Tuple[ActuatorParameter, ...] = ()
    deployment_state: ActuatorDeploymentState = ActuatorDeploymentState.UNSPECIFIED
    live_capable: bool = False
    live_backend: Optional[str] = None
    live_blocking_premise: Optional[str] = None
    provenance_note: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "service_model", frozen_mapping(self.service_model))
        object.__setattr__(self, "parameters", frozen_tuple(self.parameters))


@dataclass(frozen=True)
class DeploymentBinding(ContractIdentity):
    """How to reach one endpoint in this deployment, without any secret.

    Attributes
    ----------
    endpoint_id:
        Logical endpoint name, e.g. ``"r1"``, ``"nonrt"``, ``"o1-provider"``.
    base_url:
        Address only.  Never a URL with embedded credentials.
    transport_security:
        What the endpoint requires.
    secret_refs:
        Named *references* the runtime resolves -- ``{"clientSecret":
        "env:R1_CLIENT_SECRET"}``.  Never a value.
    trust_anchor_ref:
        Reference to the trust anchor / CA bundle for mTLS.
    software_versions:
        Provenance for the paper's environment record (design section 13).
    """

    endpoint_id: str
    base_url: str
    transport_security: TransportSecurity
    secret_refs: Mapping[str, str]
    trust_anchor_ref: Optional[str] = None
    software_versions: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "secret_refs", frozen_mapping(self.secret_refs))
        object.__setattr__(self, "software_versions", frozen_mapping(self.software_versions))


@dataclass(frozen=True)
class CapabilityManifest(ContractIdentity):
    """What one deployed capability can actually do.

    ``supported_objectives`` is honest advertising, not aspiration: design
    section 10 requires unsupported fields and missing KPI, actuator, rollback
    or evidence paths to fail closed and "remain honestly advertised".  A
    capability that lists an objective it cannot read back is a contract
    validation failure, not a documentation problem.
    """

    capability_id: str
    #: Project objective identifiers this capability can serve.
    supported_objectives: Tuple[str, ...]
    #: Constraints on what it can be asked for.
    constraints: Tuple[TypedConstraint, ...]
    #: Actuator binding ids this capability actuates through.
    actuator_refs: Tuple[str, ...]
    #: Measurement contract ids this capability's effects are observed by.
    measurement_refs: Tuple[str, ...]
    #: Published standard/interface versions this capability implements.
    interface_versions: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(
            self, "supported_objectives", frozen_tuple(self.supported_objectives)
        )
        object.__setattr__(self, "constraints", frozen_tuple(self.constraints))
        object.__setattr__(self, "actuator_refs", frozen_tuple(self.actuator_refs))
        object.__setattr__(self, "measurement_refs", frozen_tuple(self.measurement_refs))
        object.__setattr__(self, "interface_versions", frozen_mapping(self.interface_versions))


@dataclass(frozen=True)
class CompositionManifest(ContractIdentity):
    """Which capabilities are deployed together, and what that forbids.

    A composition exists because capabilities interact: two xApps that both
    steer the same UE are individually valid and jointly unsafe.
    ``mutual_exclusions`` and ``joint_constraints`` are where that is stated,
    and they are frozen into the epoch with everything else so a composition
    cannot change under a running trial (design section 6.3).
    """

    composition_id: str
    capability_refs: Tuple[str, ...]
    #: Pairs of capability ids that must not be active simultaneously.
    mutual_exclusions: Tuple[Tuple[str, str], ...] = ()
    #: Constraints that hold only for the combination.
    joint_constraints: Tuple[TypedConstraint, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "capability_refs", frozen_tuple(self.capability_refs))
        object.__setattr__(
            self,
            "mutual_exclusions",
            tuple(tuple(pair) for pair in frozen_tuple(self.mutual_exclusions)),
        )
        object.__setattr__(
            self, "joint_constraints", frozen_tuple(self.joint_constraints)
        )
