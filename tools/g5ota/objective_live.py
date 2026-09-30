"""What a live objective family needs beyond its contract bundle.

:func:`assurance.live.objective_runtime.build_live_objective_runtime` already
turns any family's frozen bundle into the same
:class:`~assurance.vertical.VerticalPath` the Gate 3 pin-to-cell case used.
Three small things sit outside it, and they sit here rather than inside it
because each is about the *console* rather than about the contracts:

``family_grammar`` / ``family_utterance``
    The sentence an Operator types, and the deterministic grammar entry that
    reads it.  Gate 3 wrote both longhand for one objective.  A family's entry
    is derived from its own bundle -- the measurement the constraint binds to
    is the bundle's own ``-min`` contract, not a name repeated here -- so a
    family whose measurements are renamed cannot end up with a grammar that
    points at a contract the epoch does not carry.

``bundle_contracts``
    :class:`tools.g3ota.composition.LivePolicyBuilder` was written against the
    six contracts the Gate 3 case named in a dict.  A bundle carries the same
    six, so this is a projection, not a translation: it selects, it never
    substitutes, and it refuses a bundle whose shape it cannot select from
    rather than picking the first of several.

``live_scope``
    The scope the bundle is built for, assembled from what was *observed* --
    the UE identity off the live indication stream -- and what the binding
    already committed, the two cell identities.  Nothing here is a setting.

None of it decides anything.  The registry decides whether a family may be
submitted at all (``tools.g3ota.objectives.resolve_submittable_family``), and
the Kernel decides everything after the sentence is read.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping

from assurance.advisors.grammar import IntentGrammarEntry
from assurance.contracts.target import ComparisonOperator
from assurance.live.pin_to_cell_driver import NCI_UNIT

__all__ = [
    "FAMILY_UTTERANCES",
    "BundleDirection",
    "ObjectiveWiringError",
    "bundle_contracts",
    "bundle_direction",
    "family_grammar",
    "family_utterance",
    "live_scope",
    "objective_dry_run_plan",
]

#: One sentence per family, and the keywords that select it.
#:
#: The keywords are checked case-insensitively as substrings of the utterance,
#: so each family's sentence contains its own keyword and no other family's.
#: The cell identity precedes the scope token for the reason Gate 3 recorded:
#: the deterministic grammar takes the *first* number in the sentence as the
#: drafted bound, and a UE label containing a digit would otherwise be read as
#: the cell.
FAMILY_UTTERANCES: Mapping[str, Mapping[str, Any]] = {
    "TrafficSteeringPreference": {
        "keywords": ("steering preference", "steer"),
        "template": "Steer the UE to serving cell {nci} nci for ueId={ue}",
    },
    "UELevelTarget": {
        "keywords": ("ue-level", "ue level"),
        "template": "Hold the UE-level serving cell at {nci} nci for ueId={ue}",
    },
    "QoSTarget": {
        "keywords": ("qos target", "qos by cell selection"),
        "template": (
            "Meet the QoS target by cell selection at serving cell {nci} nci "
            "for ueId={ue}"
        ),
    },
    "QoSandTSP": {
        "keywords": ("qos and tsp", "joint qos steering"),
        "template": (
            "Meet QoS and TSP together by cell selection at serving cell "
            "{nci} nci for ueId={ue}"
        ),
    },
}


class ObjectiveWiringError(RuntimeError):
    """The family's bundle is not the shape the live console can drive."""


def _one(candidates: Any, what: str) -> Any:
    items = list(candidates)
    if len(items) != 1:
        raise ObjectiveWiringError(
            "expected exactly one %s in the bundle, found %d" % (what, len(items)))
    return items[0]


def _measurement(bundle: Any, suffix: str) -> Any:
    return _one(
        (m for m in bundle.measurements if str(m.contract_id).endswith(suffix)),
        "measurement contract ending %r" % suffix,
    )


def family_grammar(family: str, bundle: Any) -> Mapping[str, IntentGrammarEntry]:
    """The one grammar entry this run registers with the Intent Agent.

    Exactly one, deliberately.  The agent selects an objective from whatever
    registry it is handed; handing it every family would let a sentence
    written for one objective select another, and the point of naming the
    family on the command line is that the Operator said which one.
    """
    try:
        spec = FAMILY_UTTERANCES[family]
    except KeyError:
        raise ObjectiveWiringError(
            "no operator sentence is defined for %s; a family goes OTA with a "
            "sentence a person could have typed, not with one this runner "
            "invented at submit time" % family) from None
    return {
        family: IntentGrammarEntry(
            objective_family=family,
            keywords=tuple(spec["keywords"]),
            measurement_ref=str(_measurement(bundle, "-min").contract_id),
            default_operator=ComparisonOperator.EQUAL,
            default_unit=NCI_UNIT,
        )
    }


def family_utterance(family: str, target_nci: int, ue_scope_id: str) -> str:
    try:
        spec = FAMILY_UTTERANCES[family]
    except KeyError:
        raise ObjectiveWiringError(
            "no operator sentence is defined for %s" % family) from None
    return str(spec["template"]).format(nci=int(target_nci), ue=str(ue_scope_id))


def bundle_contracts(bundle: Any) -> Dict[str, Any]:
    """The six contracts :class:`LivePolicyBuilder` derives a policy from."""
    return {
        "measurement_min": _measurement(bundle, "-min"),
        "measurement_max": _measurement(bundle, "-max"),
        "target": bundle.target,
        "case_policy": bundle.case_policy,
        "harm": bundle.harm,
        "actuator": _one(bundle.actuators, "actuator binding"),
    }


class BundleDirection(tuple):
    """``(baselineNci, targetNci)`` -- what the built bundle actually expresses.

    Read off the bundle rather than off the command line, because the two can
    disagree and only one of them is the contract.  ``TrafficSteeringPreference``
    fixes its cell pair in module constants (``_HOME_NCI`` / ``_TARGET_NCI``) and
    ignores the scope's ``homeServingCell`` / ``targetServingCell`` entirely, so
    a bundle built for the reverse direction comes back expressing the forward
    one.  ``UELevelTarget`` honours the scope.  Asking the bundle is the only
    reading that is true for both.

    The consequence is worth stating plainly: TSP as frozen can only be
    submitted while the UE is on 12345678.  Making it directional is a family
    contract change, and is not taken by a runner.
    """

    @property
    def baseline(self) -> int:
        return int(self[0])

    @property
    def target(self) -> int:
        return int(self[1])


def bundle_direction(bundle: Any) -> BundleDirection:
    """Which cell the bundle starts from and which single cell it may move to."""
    axis = "servingCell"
    try:
        baseline = int(str(bundle.baseline_config[axis]))
    except (KeyError, TypeError, ValueError):
        raise ObjectiveWiringError(
            "the bundle states no %s baseline" % axis) from None
    option = _one(bundle.target.options, "target option")
    values = tuple(option.parameter_space.get(axis, ()))
    if len(values) != 1:
        raise ObjectiveWiringError(
            "the bundle's target names %d %s values; this runner submits one"
            % (len(values), axis))
    return BundleDirection((baseline, int(str(values[0]))))


def live_scope(*, amf_ue_ngap_id: int, home_nci: int, target_nci: int,
               cell_id: str = "NRCellDU-1") -> Dict[str, Any]:
    """The scope a family's bundle is built for on this deployment.

    ``ueId`` is the identity observed on the live stream at submit time, not a
    configured one.  A family that scopes to the cell as well as the UE reads
    ``cellId``; one that scopes to the UE alone ignores it.
    """
    return {
        "ueId": str(int(amf_ue_ngap_id)),
        "cellId": str(cell_id),
        "targetServingCell": str(int(target_nci)),
        "homeServingCell": str(int(home_nci)),
    }


def objective_dry_run_plan(family_module: Any, bundle: Any) -> Dict[str, Any]:
    """Validate and describe a family drive without observing or submitting.

    This is deliberately a projection of the real registry, family module and
    contract bundle.  It does not instantiate the live runtime, reader,
    producer client or policy port, so ``externalCalls`` is a structural zero
    rather than a counter reset after a call.
    """
    from assurance.objectives.registry import (
        ASSURANCE_FAMILY_TO_WIRE_KIND,
        ASSURANCE_FAMILY_WIRE_KIND_MAPPING_VERSION,
        record_for,
    )

    family = str(family_module.family)
    record = record_for(family)
    lifecycle = family_module.policy_lifecycle()
    try:
        wire_kind = ASSURANCE_FAMILY_TO_WIRE_KIND[family]
    except KeyError:
        raise ObjectiveWiringError(
            f"{family} has no assurance-family to wire-kind mapping"
        ) from None
    if not record.deployment_capability.submittable:
        raise ObjectiveWiringError(f"{family} is not registry-submittable")
    if lifecycle.policy_type_id is None:
        raise ObjectiveWiringError(f"{family} declares no policy type")
    contracts = bundle_contracts(bundle)
    actuator = contracts["actuator"]
    if actuator.policy_type_id != lifecycle.policy_type_id:
        raise ObjectiveWiringError(
            f"{family} lifecycle and actuator name different policy types"
        )
    if bundle.configuration_axes() != ("servingCell",):
        raise ObjectiveWiringError(
            f"{family} dry-run requires the single servingCell axis"
        )
    service_model = dict(actuator.service_model)
    if (service_model.get("style"), service_model.get("action")) != ("3", "1"):
        raise ObjectiveWiringError(
            f"{family} is not bound to E2SM-RC Style 3 / Action 1 steering"
        )

    mandatory = tuple(
        predicate.predicate_id for predicate in bundle.target.predicates
    )
    component_predicates = {
        str(component): list(predicate_ids)
        for component, predicate_ids in bundle.component_predicates.items()
    }
    named_components = {
        predicate_id
        for predicate_ids in component_predicates.values()
        for predicate_id in predicate_ids
    }
    if named_components and named_components != set(mandatory):
        raise ObjectiveWiringError(
            f"{family} component predicates do not equal its mandatory target set"
        )

    direction = bundle_direction(bundle)
    return {
        "mode": "HARDWARE_FREE_DRY_RUN",
        "objective": family,
        "wireKindMappingVersion": ASSURANCE_FAMILY_WIRE_KIND_MAPPING_VERSION,
        "wireKind": wire_kind,
        "policyTypeId": lifecycle.policy_type_id,
        "actuator": {
            "contractId": actuator.contract_id,
            "path": actuator.path.value,
            "serviceModel": service_model,
            "meaning": "cell steering; QoS is achieved through cell selection",
        },
        "configurationAxes": list(bundle.configuration_axes()),
        "baselineConfiguration": dict(bundle.baseline_config),
        "targetConfiguration": {"servingCell": str(direction.target)},
        "targetOptionCount": len(bundle.target.options),
        "mandatoryPredicates": list(mandatory),
        "componentPredicates": component_predicates,
        "jointTrialRequired": record.joint_trial_required,
        "measurementSources": [
            {
                "counter": counter.deployment_counter_name,
                "source": counter.source.value,
                "scopeKeys": list(counter.scope_keys),
            }
            for counter in bundle.counters
        ],
        "externalCalls": 0,
    }
