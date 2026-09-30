"""What this composition may advertise, let an operator select, and submit.

The frozen ``AIC_UECellSteering_1.0.0`` policy schema can express two steering
objectives.  The composed release ``oran-aic-lower-integration/1.0.0`` advertises
exactly one of them as official - ``PIN_TO_CELL`` - and states in the same
manifest that ``BALANCE_PRB_LOAD`` is not advertised and that its Style 2 /
Action 6 QoS work is an experimental extension with **no** A1 policy type.

This module is where those three facts meet:

* :data:`EXECUTABLE_OBJECTIVES` is the deployment allowlist for the **final**
  composition.  It is a constant, not a manifest read, so a capability document
  that advertised more could not widen it.
* :func:`advertise` applies it whenever the composition is bound to a composed
  release, intersected with that release's official objectives and with the
  deployment's own capability manifest.  Anything outside the intersection is
  returned as a *non-executable* advertisement carrying the reason, so a console
  can show why an objective is unavailable instead of silently omitting it.

A composition with no composed release bound is not the final composition - it is a
development deployment, which is what the loopback profile and the conformance
catalog use.  Such a composition is limited to what its own capability manifest
advertises and is *labelled* as unbound in
:attr:`AdvertisedComposition.basis`, rather than being quietly treated as Live.
Binding the composed release is what narrows the composition to ``PIN_TO_CELL``,
and binding it is what a Live deployment does.
* :func:`assert_submittable` is the gate.  It runs before any R1 call, so an
  objective the composition does not advertise never reaches a producer.

The experimental extension is handled separately and deliberately: it is
returned by :func:`advertise` as an :class:`ExtensionAdvertisement`, never as an
objective, and :func:`assert_submittable` refuses it by name.  A composed release
that ever claimed an A1 policy type for it, or claimed it was mixed into the
frozen cell-steering policy, fails the composition closed at advertisement time
rather than producing a policy that mixes the two.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from .deployment_binding import DeploymentBindingContracts, DeploymentBindingError

#: Objectives this deployment will execute.  One entry, by decision of the
#: integration authority: ``PIN_TO_CELL`` is the only officially supported
#: objective of the frozen combination.
EXECUTABLE_OBJECTIVES: Tuple[str, ...] = ("PIN_TO_CELL",)

#: Objectives the frozen policy schema can represent at all.  Used only to
#: explain a refusal; it never widens :data:`EXECUTABLE_OBJECTIVES`.
SCHEMA_OBJECTIVES: Tuple[str, ...] = ("BALANCE_PRB_LOAD", "PIN_TO_CELL")


class ObjectiveNotExecutable(ValueError):
    """The requested objective is not executable by this composition."""


@dataclass(frozen=True)
class ObjectiveAdvertisement:
    """One objective, with whether it may be selected and why."""

    kind: str
    executable: bool
    state: str
    reason: Optional[str] = None
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        record = {"kind": self.kind, "executable": self.executable,
                  "state": self.state}
        if self.reason:
            record["reason"] = self.reason
        if self.provenance:
            record["provenance"] = dict(self.provenance)
        return record


@dataclass(frozen=True)
class ExtensionAdvertisement:
    """A composed-release capability that is evidence, not executable."""

    name: str
    state: str
    service_model: Optional[str] = None
    control_axis: Optional[str] = None
    a1_policy_type: Optional[str] = None
    reason: str = ""
    provenance: Mapping[str, Any] = field(default_factory=dict)

    #: Fixed for every extension: an extension is never an objective.
    executable: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "state": self.state, "executable": False,
            "serviceModel": self.service_model, "controlAxis": self.control_axis,
            "a1PolicyType": self.a1_policy_type, "reason": self.reason,
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True)
class AdvertisedComposition:
    """Everything an operator surface may offer for one bound deployment."""

    objectives: Tuple[ObjectiveAdvertisement, ...]
    extensions: Tuple[ExtensionAdvertisement, ...]
    composed_release_bound: bool
    basis: str

    @property
    def executable(self) -> Tuple[str, ...]:
        return tuple(item.kind for item in self.objectives if item.executable)

    @property
    def final(self) -> bool:
        """Whether this is the final composition rather than a development one.

        Only a deployment-bound composition is the standard end-to-end path, and
        only a deployment-bound composition is narrowed to
        :data:`EXECUTABLE_OBJECTIVES`.  A surface that offers a Live session has
        to be able to tell the two apart without inferring it from an endpoint.
        """
        return self.composed_release_bound

    def as_dict(self) -> Dict[str, Any]:
        return {
            "objectives": [item.as_dict() for item in self.objectives],
            "extensions": [item.as_dict() for item in self.extensions],
            "executable": list(self.executable),
            "composedReleaseBound": self.composed_release_bound,
            "final": self.final,
            "basis": self.basis,
        }


def _capability_objectives(capability_manifest: Mapping[str, Any]) -> Tuple[str, ...]:
    advertised = capability_manifest.get("objectives")
    if not isinstance(advertised, (list, tuple)):
        raise ObjectiveNotExecutable(
            "the deployment's capability manifest advertises no objectives")
    return tuple(str(item) for item in advertised)


def _extensions(binding: DeploymentBindingContracts) -> Tuple[ExtensionAdvertisement, ...]:
    """Project a composed release's experimental extensions, fail-closed.

    The composition refuses to advertise anything at all if an extension claims
    an A1 policy type or claims to be mixed into the frozen cell-steering
    policy.  That is not a display concern: it would mean the composed release
    had changed the binding relationship, and continuing
    would risk emitting a UE Cell Steering policy carrying QoS semantics.
    """
    result = []
    for name, value in sorted(binding.experimental_extensions.items()):
        if not isinstance(value, Mapping):
            raise DeploymentBindingError(
                f"experimental extension {name} is not a capability object")
        policy_type = value.get("a1PolicyType")
        if policy_type not in (None, "NONE"):
            raise DeploymentBindingError(
                f"experimental extension {name} claims A1 policy type "
                f"{policy_type!r}; this composition executes only the frozen "
                "AIC_UECellSteering_1.0.0 policy type and refuses to compose "
                "against a release that binds an experimental extension to one")
        if value.get("mixedIntoFrozenCellSteeringPolicy") is not False:
            raise DeploymentBindingError(
                f"experimental extension {name} does not state that it is kept "
                "out of the frozen cell-steering policy")
        result.append(ExtensionAdvertisement(
            name=str(name), state=str(value.get("state") or "EXPERIMENTAL"),
            service_model=(str(value["serviceModel"])
                           if value.get("serviceModel") else None),
            control_axis=(str(value["controlAxis"])
                          if value.get("controlAxis") else None),
            a1_policy_type=None,
            reason=("capability and provenance only: this extension has no "
                    "frozen A1 policy type, so no policy can be generated or "
                    "executed for it from this composition"),
            provenance=dict(value)))
    return tuple(result)


def advertise(*, capability_manifest: Mapping[str, Any],
              lower: Optional[DeploymentBindingContracts] = None
              ) -> AdvertisedComposition:
    """Return the objectives and extensions this composition may offer.

    ``lower`` is the compatibility parameter for the verified composed release.
    When it is present the composition
    is the final O-RAN runtime composition and the executable set is the
    intersection of the deployment allowlist, the deployment capability manifest
    and the composed release's official objectives - one entry, ``PIN_TO_CELL``. When
    it is absent the composition is a development deployment that is not bound
    to a composed release; that is stated in :attr:`AdvertisedComposition.basis`
    rather than hidden, and such a composition is not the Live path.
    """
    capability = _capability_objectives(capability_manifest)
    extensions: Tuple[ExtensionAdvertisement, ...] = ()
    official: Optional[Tuple[str, ...]] = None
    if lower is not None:
        extensions = _extensions(lower)
        official = lower.official_objectives
        if not official:
            raise DeploymentBindingError(
                "the composed-release capability manifest advertises no official objective")

    # The final composition is the narrow one; a development deployment is
    # limited by its own capability manifest and says so.
    allowlist = EXECUTABLE_OBJECTIVES if lower is not None else SCHEMA_OBJECTIVES
    objectives = []
    for kind in sorted(set(capability) | set(SCHEMA_OBJECTIVES)):
        provenance: Dict[str, Any] = {"advertisedByCapabilityManifest":
                                      kind in capability}
        if official is not None:
            provenance["officialInComposedRelease"] = kind in official
        if kind not in allowlist:
            objectives.append(ObjectiveAdvertisement(
                kind=kind, executable=False, state="NOT_EXECUTABLE",
                reason=("the final O-RAN composition executes only "
                        f"{', '.join(EXECUTABLE_OBJECTIVES)}"),
                provenance=provenance))
            continue
        if kind not in capability:
            objectives.append(ObjectiveAdvertisement(
                kind=kind, executable=False, state="NOT_ADVERTISED",
                reason="the deployment's capability manifest does not advertise it",
                provenance=provenance))
            continue
        if official is not None and kind not in official:
            objectives.append(ObjectiveAdvertisement(
                kind=kind, executable=False, state="NOT_OFFICIAL",
                reason="the bound composed release does not advertise it as official",
                provenance=provenance))
            continue
        objectives.append(ObjectiveAdvertisement(
            kind=kind, executable=True, state="EXECUTABLE", provenance=provenance))

    if lower is not None:
        basis = (f"COMPOSED_RELEASE_BOUND:{lower.identity.release}"
                 f"@{lower.identity.capability_manifest_sha256[:16]}")
    else:
        basis = "DEVELOPMENT_DEPLOYMENT_NOT_COMPOSED_RELEASE_BOUND"
    return AdvertisedComposition(
        objectives=tuple(objectives), extensions=extensions,
        composed_release_bound=lower is not None, basis=basis)


def assert_submittable(objective_kind: Any, *,
                       capability_manifest: Mapping[str, Any],
                       lower: Optional[DeploymentBindingContracts] = None) -> str:
    """Refuse, by name, any objective this composition does not advertise.

    Runs before the first R1 call, so a refused objective produces no bootstrap,
    no discovery, no policy and no write.  The returned value is the accepted
    objective kind, so callers can use it in place of the raw input.
    """
    kind = str(objective_kind or "")
    if not kind:
        raise ObjectiveNotExecutable(
            "the policy context states no objectiveKind; this composition does "
            "not default one")
    composition = advertise(capability_manifest=capability_manifest, lower=lower)
    for extension in composition.extensions:
        if kind in (extension.name, extension.service_model,
                    extension.control_axis):
            raise ObjectiveNotExecutable(
                f"{kind} is the experimental extension {extension.name}, which "
                "has no frozen A1 policy type; it is capability and provenance "
                "only and cannot be submitted as a UE Cell Steering objective")
    for advertisement in composition.objectives:
        if advertisement.kind != kind:
            continue
        if advertisement.executable:
            return kind
        raise ObjectiveNotExecutable(
            f"{kind} is not executable by this composition "
            f"({advertisement.state}): {advertisement.reason}")
    raise ObjectiveNotExecutable(
        f"{kind} is not an objective of the frozen AIC_UECellSteering_1.0.0 "
        f"policy type; executable objectives are {list(composition.executable)}")


__all__ = ["AdvertisedComposition", "EXECUTABLE_OBJECTIVES",
           "ExtensionAdvertisement", "ObjectiveAdvertisement",
           "ObjectiveNotExecutable", "SCHEMA_OBJECTIVES", "advertise",
           "assert_submittable"]
