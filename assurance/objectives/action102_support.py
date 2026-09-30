"""SUPPLEMENTARY E2SM-RC Style-2 actions for the families that may carry them.

Owner lane: **OBJ1/OBJ2** (``docs/architecture/SEAMS-GATE4.md`` section 3).

This module is *not* an objective family.  ``ue-dl-prb-cap`` (Action 102) and
``scheduler-priority`` (Action 103) select no candidate, state no objective and
cannot run standalone; they supplement the PRIMARY steering action of
``QoSTarget`` or ``UELevelTarget``.  There is therefore no
``ObjectiveFamilyModule`` here and no registry record: what this module does is
take a family's already-built bundle and add the four things a supplementary
action needs before the Kernel could admit one.

**A finite candidate catalog.**  The values are added to the family's existing
target option, so the epoch freezes ``|cells| x |values|`` candidates under one
catalog hash.  Each axis is a scalar in text, exactly as the steering axis is
the bare cell identity in text: the Kernel refuses a plan whose steps do not
*literally* realise the frozen candidate, so an encoding between the candidate
parameter and the plan step would be a place for a plan to drift from what it
claims to be.  :func:`supplementary_axis_declarations` adds the one thing a
candidate parameter cannot carry -- which registered client writes it.

**Two separately charged scopes.**  For a cap, the objective UE's throughput
floor becomes a mandatory predicate and a ``STOP_AND_ROLLBACK`` watchdog: a cap
that could take the very UE the target protects below its floor is refused, not
compensated.  The controlled UE's throughput is charged to *its own* scope
through a :class:`~assurance.contracts.harm.CertifiedHarmBound`, so neither debt
can hide inside a cell aggregate.

**An honest configuration surface.**  Each axis joins the baseline and the safe
state at its own uncapped/neutral value.  A surface that did not name the axis
would make every merged readback incomplete, and a safe state that did not name
it would leave the change live after an emergency stop.

The policy types are the released Campaign 5 ones this deployment's producer
already advertises (``AIC_UeDlPrbCap_1.0.0``, ``AIC_SchedulerPriority_1.0.0``);
this module names them as data and imports nothing from ``oran``.  Nothing here
opens a transport and nothing here grants a permit: every number it produces is
an epoch-frozen contract the Kernel still has to admit.

**Three of the six are not scoped to a UE.**  ``dl-mcs-bounds`` and
``dl-rf-attenuation`` are cell-wide and ``slice-prb-quota`` is per S-NSSAI, so
each carries its own scope kind and its own readback scope key: a cell's MCS
ceiling read back under a UE key would claim to be one UE's configuration and
the merged surface would be wrong by exactly the number of UEs on that cell.
Two of them also write more than one policy leaf in a single command, so their
axis value is one composite string (``"10..28"``, ``"0:1:60"``) that
:meth:`SupplementaryAction.policy_values` decodes -- one axis, one write, no
intermediate configuration nobody asked for.  Whether the deployment's action
producer actually serves an axis's policy type is a *live* question the
composition root asks; this module only states what an epoch would freeze.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Dict, Mapping, Sequence, Tuple

from assurance.actions.catalog import (
    UE_DL_PRB_CAP_APPLY_RANGE, UE_DL_PRB_CAP_UNCAPPED_SENTINEL,
)
from assurance.actions.composition_policy import (
    LIVE_CAP_FAMILIES, live_cap_admissible,
)
from assurance.contracts.capability import DeploymentBinding
from assurance.contracts.harm import (
    CertifiedHarmBound, WatchdogAction, WatchdogContract,
)
from assurance.contracts.measurement import (
    Aggregation, ClockRequirement, CounterBinding, Estimator, GapPolicy,
    MeasurementContract, MeasurementSource, OverlapPolicy, UncertaintyRule,
)
from assurance.contracts.target import (
    ComparisonOperator, TargetPredicate, TypedConstraint,
)
from assurance.core.envelopes import ASSURANCE_SCHEMA_VERSION
from assurance.core.provenance import Provenance, TypedQuantity

__all__ = [
    "ATTENUATION_ACTION_ID",
    "ATTENUATION_ADAPTER_KEY",
    "ATTENUATION_AXIS",
    "ATTENUATION_CONFIGURATION_COUNTER",
    "ATTENUATION_DECIMALS",
    "ATTENUATION_MAXIMUM_DB",
    "ATTENUATION_MINIMUM_DB",
    "ATTENUATION_POLICY_TYPE_ID",
    "CAP_ACTION_ID",
    "CAP_ADAPTER_KEY",
    "CAP_AXIS",
    "CAP_CONFIGURATION_COUNTER",
    "CAP_MAXIMUM_PRB",
    "CAP_MINIMUM_PRB",
    "CAP_POLICY_TYPE_ID",
    "CAP_UNIT",
    "CELL_SCOPE_KEY",
    "CONTROLLED_UE_SCOPE_KEY",
    "LIVE_CAP_FAMILIES",
    "MCS_ACTION_ID",
    "MCS_ADAPTER_KEY",
    "MCS_AXIS",
    "MCS_CONFIGURATION_COUNTER",
    "MCS_MAXIMUM_INDEX",
    "MCS_MINIMUM_INDEX",
    "MCS_POLICY_TYPE_ID",
    "PRIORITY_ACTION_ID",
    "PRIORITY_ADAPTER_KEY",
    "PRIORITY_AXIS",
    "PRIORITY_POLICY_TYPE_ID",
    "SLICE_QUOTA_ACTION_ID",
    "SLICE_QUOTA_ADAPTER_KEY",
    "SLICE_QUOTA_AXIS",
    "SLICE_QUOTA_CONFIGURATION_COUNTER",
    "SLICE_QUOTA_MAXIMUM_PERCENT",
    "SLICE_QUOTA_MINIMUM_PERCENT",
    "SLICE_QUOTA_POLICY_TYPE_ID",
    "SLICE_SCOPE_KEY",
    "SUPPLEMENTARY_ACTIONS",
    "SupplementaryAction",
    "SupplementaryCapError",
    "SupplementaryCapRequest",
    "cap_axis_baseline",
    "cap_candidate_values",
    "live_cap_admissible",
    "supplementary_action",
    "supplementary_axis_declarations",
    "with_supplementary_cap",
]

#: The configuration-surface axis the cap moves, and the name a frozen
#: candidate carries it under.  The same name for both: see the module
#: docstring.
CAP_AXIS = "dlPrbCap"
CAP_ACTION_ID = "ue-dl-prb-cap"
#: The Write Gateway adapter permanently bound to the cap policy type.
CAP_ADAPTER_KEY = "r1-cap"
#: The released Campaign 5 policy type this deployment's producer advertises.
CAP_POLICY_TYPE_ID = "AIC_UeDlPrbCap_1.0.0"
CAP_UNIT = "PRB"
CAP_CONFIGURATION_COUNTER = "RAN.UE.DlPrbCap"
CAP_MINIMUM_PRB, CAP_MAXIMUM_PRB = UE_DL_PRB_CAP_APPLY_RANGE
#: ``0`` is not a cap of zero PRB -- it is "no cap", and it has to pass this
#: check.  The band's own reason is the scheduler's ("no new-data grant below
#: five"), which speaks to 1..4 and says nothing about removing the cap, and
#: three independent records agree that 0 is an applied value here:
#:
#:   * the deployed producer's own AIC_UeDlPrbCap_1.0.0 schema --
#:     ``"maxDlPrbs": {"minimum": 0, ... "0 removes the cap."}``
#:   * ``oran/campaign5/live_worker.py::_restore``, which sends the captured
#:     baseline ``{"maxDlPrbs": 0}`` as an RC control and waits to read exactly
#:     that back -- every cap rollback this bed has ever done
#:   * ``oai_patches/d2_actionspace_runtime_knobs.patch``: "0 = uncapped
#:     (default, full carrier)"
#:
#: And ``assurance/actions/catalog.py``'s copy of this rule already exempted the
#: sentinel; only this one forgot, so the two enforcement points disagreed.  The
#: cost of the disagreement was measured on 2026-09-17: every v4 episode ended
#: CATALOG_EXHAUSTED the moment a trial settled a cap, because no candidate
#: could name the uncapped baseline again.
CAP_UNCAPPED_SENTINEL = UE_DL_PRB_CAP_UNCAPPED_SENTINEL

PRIORITY_AXIS = "pfWeight"
PRIORITY_ACTION_ID = "scheduler-priority"
PRIORITY_ADAPTER_KEY = "r1-priority"
PRIORITY_POLICY_TYPE_ID = "AIC_SchedulerPriority_1.0.0"
PRIORITY_CONFIGURATION_COUNTER = "RAN.UE.PfWeight"

#: The three cell- and slice-scoped actions this deployment can also compose.
#: Every string below is the one the deployment already carries: the policy
#: type, adapter, RC style/action and configuration counter are the Campaign 5
#: families of ``oran/campaign5/families.py`` (``mcs``, ``power``) and the
#: slice actuator's ``oran/slice_actuator/a1.py``; the parameters, their ranges
#: and their cross-field rules are ``assurance/actions/catalog.py``; the wire
#: grammar is ``assurance/xapps/live_actuation.py``.  They are named here as
#: data, not imported, for the same reason the cap's are: this module states
#: what an epoch freezes and imports nothing from ``oran``.  A test pins the
#: agreement so the two cannot drift apart silently.
MCS_AXIS = "dlMcsBounds"
MCS_ACTION_ID = "dl-mcs-bounds"
MCS_ADAPTER_KEY = "r1-mcs"
MCS_POLICY_TYPE_ID = "AIC_DlMcsBounds_1.0.0"
MCS_CONFIGURATION_COUNTER = "RAN.Cell.DlMcsBounds"
MCS_MINIMUM_INDEX, MCS_MAXIMUM_INDEX = 0, 28

ATTENUATION_AXIS = "txAttenuationDb"
ATTENUATION_ACTION_ID = "dl-rf-attenuation"
ATTENUATION_ADAPTER_KEY = "r1-power"
ATTENUATION_POLICY_TYPE_ID = "AIC_CellDlTxPower_1.0.0"
#: *Not* ``L1M.SS-RSRP``.  The catalog names the RSRP the attenuation moves --
#: an effect -- and a supplementary axis needs the configuration readback that
#: proves the write landed, which is the Campaign 5 family's own counter.
ATTENUATION_CONFIGURATION_COUNTER = "RAN.Cell.TxAttenuationDb"
#: ``rfatt_cmd`` accepts ``[0,60] dB`` and prints ``%.1f``; a value with more
#: resolution cannot round trip, so it is refused before any write.
ATTENUATION_MINIMUM_DB, ATTENUATION_MAXIMUM_DB = 0.0, 60.0
ATTENUATION_DECIMALS = 1

SLICE_QUOTA_AXIS = "slicePrbQuota"
SLICE_QUOTA_ACTION_ID = "slice-prb-quota"
SLICE_QUOTA_ADAPTER_KEY = "r1-slice"
SLICE_QUOTA_POLICY_TYPE_ID = "AIC_SliceSLATarget_1.0.0"
SLICE_QUOTA_CONFIGURATION_COUNTER = "RAN.SlicePrbQuotaMin"
SLICE_QUOTA_MINIMUM_PERCENT, SLICE_QUOTA_MAXIMUM_PERCENT = 0, 100

#: Where the plan scope carries the controlled non-target UE.  A *different*
#: key from the objective UE's, so the two can never be told apart by position.
CONTROLLED_UE_SCOPE_KEY = "controlledUe"
#: The measurement scope key each scope kind is read back under.  A cell-wide
#: or slice-wide configuration is not charged to a UE, so it cannot share the
#: UE key: a readback keyed by ``controlledUeId`` would claim a cell's MCS
#: ceiling was one UE's, and the merged surface would be wrong by exactly the
#: number of UEs on that cell.
CONTROLLED_UE_MEASUREMENT_KEY = "controlledUeId"
CELL_SCOPE_KEY = "cellId"
SLICE_SCOPE_KEY = "sNssai"

_THROUGHPUT_COUNTER = "DRB.UEThpDl"


class SupplementaryCapError(ValueError):
    """A supplementary action cannot be composed onto this family or bundle."""


# --------------------------------------------------------------------------- #
# composite axis values
# --------------------------------------------------------------------------- #
#
# A frozen candidate parameter is one sortable scalar in text -- the Kernel
# refuses a plan whose steps do not *literally* realise the candidate, so the
# candidate and the axis have to be the same string.  Three of the six actions
# carry more than one configuration leaf, and each of them writes all of its
# leaves in one command: ``ci mcs <max> <min>`` sets both MCS bounds, and one
# RRM Policy Ratio List carries dedicated/min/max together.  Splitting those
# into one axis per leaf would mean two or three writes and an intermediate
# configuration nobody asked for, so the axis stays one string and the
# functions below are the only place that string becomes policy leaves.


def _decode_mcs_bounds(text: Any) -> Dict[str, int]:
    """``"10..28"`` -> ``{"minDlMcs": 10, "maxDlMcs": 28}``."""
    low, separator, high = str(text).partition("..")
    if not separator:
        raise SupplementaryCapError(
            f"{MCS_ACTION_ID}: an axis value is '<min>..<max>', got {text!r}")
    try:
        minimum, maximum = int(low), int(high)
    except ValueError:
        raise SupplementaryCapError(
            f"{MCS_ACTION_ID}: both MCS bounds are integers, got {text!r}"
        ) from None
    for name, value in (("minDlMcs", minimum), ("maxDlMcs", maximum)):
        if not MCS_MINIMUM_INDEX <= value <= MCS_MAXIMUM_INDEX:
            raise SupplementaryCapError(
                f"{MCS_ACTION_ID}: {name} {value} is outside the frozen "
                f"[{MCS_MINIMUM_INDEX},{MCS_MAXIMUM_INDEX}] MCS index range")
    if minimum > maximum:
        raise SupplementaryCapError(
            f"{MCS_ACTION_ID}: minDlMcs {minimum} exceeds maxDlMcs {maximum}")
    return {"minDlMcs": minimum, "maxDlMcs": maximum}


def _encode_mcs_bounds(values: Mapping[str, Any]) -> str:
    return f"{int(values['minDlMcs'])}..{int(values['maxDlMcs'])}"


def _decode_attenuation(text: Any) -> Dict[str, float]:
    """``"6.0"`` -> ``{"txAttenuationDb": 6.0}``, at the knob's resolution."""
    try:
        value = float(str(text))
    except (TypeError, ValueError):
        raise SupplementaryCapError(
            f"{ATTENUATION_ACTION_ID}: an axis value is a number of dB, got "
            f"{text!r}") from None
    if not ATTENUATION_MINIMUM_DB <= value <= ATTENUATION_MAXIMUM_DB:
        raise SupplementaryCapError(
            f"{ATTENUATION_ACTION_ID}: txAttenuationDb {value} is outside the "
            f"[{ATTENUATION_MINIMUM_DB},{ATTENUATION_MAXIMUM_DB}] dB the knob "
            "accepts")
    scaled = value * (10 ** ATTENUATION_DECIMALS)
    if abs(scaled - round(scaled)) > 1e-9:
        raise SupplementaryCapError(
            f"{ATTENUATION_ACTION_ID}: txAttenuationDb {value} needs more than "
            f"{ATTENUATION_DECIMALS} decimal; the knob prints "
            f"%.{ATTENUATION_DECIMALS}f, so this value cannot round trip")
    return {"txAttenuationDb": value}


def _encode_attenuation(values: Mapping[str, Any]) -> str:
    return f"{float(values['txAttenuationDb']):.{ATTENUATION_DECIMALS}f}"


def _decode_slice_quota(text: Any) -> Dict[str, int]:
    """``"0:1:100"`` -> dedicated/min/max PRB policy ratios, in percent."""
    parts = str(text).split(":")
    if len(parts) != 3:
        raise SupplementaryCapError(
            f"{SLICE_QUOTA_ACTION_ID}: an axis value is "
            f"'<dedicated>:<min>:<max>' in percent, got {text!r}")
    try:
        dedicated, minimum, maximum = (int(part) for part in parts)
    except ValueError:
        raise SupplementaryCapError(
            f"{SLICE_QUOTA_ACTION_ID}: all three ratios are integer percents, "
            f"got {text!r}") from None
    values = {"dedicatedPrbPolicyRatio": dedicated,
              "minPrbPolicyRatio": minimum, "maxPrbPolicyRatio": maximum}
    for name, value in values.items():
        if not SLICE_QUOTA_MINIMUM_PERCENT <= value <= SLICE_QUOTA_MAXIMUM_PERCENT:
            raise SupplementaryCapError(
                f"{SLICE_QUOTA_ACTION_ID}: {name} {value} is outside "
                f"[{SLICE_QUOTA_MINIMUM_PERCENT},"
                f"{SLICE_QUOTA_MAXIMUM_PERCENT}] percent")
    if not dedicated <= minimum <= maximum:
        # The same cross-field rule ``validate_action_parameters`` enforces;
        # stated here too because an axis value is refused before a policy body
        # exists, not after one has been built.
        raise SupplementaryCapError(
            f"{SLICE_QUOTA_ACTION_ID}: require dedicated <= min <= max, got "
            f"{dedicated} <= {minimum} <= {maximum}")
    return values


def _encode_slice_quota(values: Mapping[str, Any]) -> str:
    return (f"{int(values['dedicatedPrbPolicyRatio'])}:"
            f"{int(values['minPrbPolicyRatio'])}:"
            f"{int(values['maxPrbPolicyRatio'])}")


#: ``value_kind`` -> (axis string -> policy leaves, policy leaves -> axis
#: string).  The scalar kinds are handled by
#: :meth:`SupplementaryAction.wire_value` and are absent here.
_COMPOSITE_KINDS: Mapping[str, Tuple[Any, Any]] = MappingProxyType({
    "mcs-bounds": (_decode_mcs_bounds, _encode_mcs_bounds),
    "attenuation-db": (_decode_attenuation, _encode_attenuation),
    "prb-quota": (_decode_slice_quota, _encode_slice_quota),
})


@dataclass(frozen=True)
class SupplementaryAction:
    """One SUPPLEMENTARY action, as frozen deployment data.

    Everything a composition needs to route a write and read it back, and
    nothing an advisory could influence: the action id, the configuration axis,
    the adapter permanently bound to it, the A1 policy type that adapter
    carries, the configuration counter that proves the effect, and the value
    the axis rests at when nothing is applied.
    """

    action_id: str
    axis: str
    adapter: str
    policy_type_id: str
    readback_counter: str
    baseline: str
    unit: str
    #: The leaf the producer's readback object carries the value under.  The
    #: gateway's configuration axis is the scalar; this names where the scalar
    #: is projected from.
    readback_leaf: str
    #: How the A1 policy body types that leaf.  The configuration axis is text
    #: -- a frozen candidate parameter is a sortable scalar -- and the wire is
    #: typed, so the composition root that builds the body coerces once, here,
    #: rather than at whichever call site notices the schema refusal first.
    #: ``mcs-bounds``, ``attenuation-db`` and ``prb-quota`` are the composite
    #: kinds: one axis string, more than one policy leaf, one write.
    value_kind: str = "integer"
    #: What the action is scoped to: a UE, a cell, or an S-NSSAI.  The three
    #: scopes are charged separately and read back under separate keys, so a
    #: composition can never present a cell-wide or slice-wide change as one
    #: UE's.
    scope_kind: str = "UE"
    #: The measurement scope key the configuration readback is keyed by.
    scope_key: str = CONTROLLED_UE_MEASUREMENT_KEY
    #: The policy leaves this action writes, in the order the schema states
    #: them.  Empty means the single :attr:`readback_leaf`.
    value_fields: Tuple[str, ...] = ()

    @property
    def leaves(self) -> Tuple[str, ...]:
        """The policy leaves one axis value decodes into."""
        return self.value_fields or (self.readback_leaf,)

    @property
    def is_composite(self) -> bool:
        return self.value_kind in _COMPOSITE_KINDS

    def wire_value(self, axis_value: Any) -> Any:
        """The axis scalar in the type the A1 policy schema requires.

        The frozen APPLY range is enforced *here*, before a body exists,
        because the released ``AIC_UeDlPrbCap_1.0.0`` schema was written for a
        275-PRB carrier and admits values this 24-PRB deployment cannot hold.
        The wire contract is the wider of the two; the deployment's own range is
        the binding one, and a value outside it is refused with zero writes
        rather than sent and read back as a mismatch.
        """
        if self.value_kind == "integer":
            value: Any = int(axis_value)
        elif self.value_kind == "number":
            value = float(axis_value)
        elif self.is_composite:
            raise SupplementaryCapError(
                f"{self.action_id}: a {self.value_kind} axis carries "
                f"{len(self.leaves)} policy leaves and has no single wire "
                "scalar; ask for policy_values instead")
        else:
            raise SupplementaryCapError(
                f"{self.action_id}: unknown value kind {self.value_kind!r}")
        if (self.action_id == CAP_ACTION_ID
                and value != CAP_UNCAPPED_SENTINEL
                and not (CAP_MINIMUM_PRB <= value <= CAP_MAXIMUM_PRB)):
            raise SupplementaryCapError(
                f"{self.action_id}: an applied cap is {CAP_MINIMUM_PRB}.."
                f"{CAP_MAXIMUM_PRB} PRB on this deployment (or "
                f"{CAP_UNCAPPED_SENTINEL} to remove the cap), got {value}")
        return value

    def policy_values(self, axis_value: Any) -> Dict[str, Any]:
        """The policy leaves one frozen axis value decodes into.

        The one place an axis string becomes typed policy content, for every
        kind: a scalar axis answers ``{leaf: wire_value}``, a composite axis
        answers all of its leaves at once.  Out-of-range and cross-field
        violations are refused *here*, before a body exists and before a permit
        is spent -- the same rules ``validate_action_parameters`` enforces on
        the body, stated at the point the candidate is realised.
        """
        if self.is_composite:
            decode, _encode = _COMPOSITE_KINDS[self.value_kind]
            return decode(axis_value)
        return {self.readback_leaf: self.wire_value(axis_value)}

    def axis_value_text(self, values: Mapping[str, Any]) -> str:
        """The inverse: policy leaves back to the axis string that froze them.

        A readback is compared against the frozen candidate as text, so the
        projection back has to be the same string the catalog carries -- not a
        second spelling of the same numbers.
        """
        if self.is_composite:
            _decode, encode = _COMPOSITE_KINDS[self.value_kind]
            missing = [leaf for leaf in self.leaves if leaf not in values]
            if missing:
                raise SupplementaryCapError(
                    f"{self.action_id}: the readback carries no "
                    + ", ".join(missing))
            return encode(values)
        if self.readback_leaf not in values:
            raise SupplementaryCapError(
                f"{self.action_id}: the readback carries no {self.readback_leaf}")
        value = values[self.readback_leaf]
        return (str(int(value)) if self.value_kind == "integer"
                else str(float(value)))

    def declaration(self) -> Dict[str, Any]:
        """How this action's candidate parameter becomes one plan step."""
        return {
            "candidateParameter": self.axis,
            "axis": self.axis,
            "adapter": self.adapter,
        }


#: The closed set of supplementary actions this deployment can compose.  A
#: closed tuple rather than a lookup by string: an action nobody declared here
#: has no adapter, no policy type and no readback counter, and inventing any of
#: the three at runtime is exactly what this table exists to prevent.
SUPPLEMENTARY_ACTIONS: Mapping[str, SupplementaryAction] = MappingProxyType({
    CAP_ACTION_ID: SupplementaryAction(
        action_id=CAP_ACTION_ID, axis=CAP_AXIS, adapter=CAP_ADAPTER_KEY,
        policy_type_id=CAP_POLICY_TYPE_ID,
        readback_counter=CAP_CONFIGURATION_COUNTER,
        baseline=str(UE_DL_PRB_CAP_UNCAPPED_SENTINEL), unit=CAP_UNIT,
        readback_leaf="maxDlPrbs", value_kind="integer",
    ),
    PRIORITY_ACTION_ID: SupplementaryAction(
        action_id=PRIORITY_ACTION_ID, axis=PRIORITY_AXIS,
        adapter=PRIORITY_ADAPTER_KEY, policy_type_id=PRIORITY_POLICY_TYPE_ID,
        readback_counter=PRIORITY_CONFIGURATION_COUNTER,
        # The scheduler's neutral proportional-fair weight.  Restoring it is
        # what "no priority is applied" means on this deployment.
        baseline="1.0", unit="ratio", readback_leaf="pfWeight",
        value_kind="number",
    ),
    MCS_ACTION_ID: SupplementaryAction(
        action_id=MCS_ACTION_ID, axis=MCS_AXIS, adapter=MCS_ADAPTER_KEY,
        policy_type_id=MCS_POLICY_TYPE_ID,
        readback_counter=MCS_CONFIGURATION_COUNTER,
        # The unconstrained link adaptation the cell runs when no bounds are
        # applied: the whole index range, which is what a pre-policy readback
        # reports and what reverse rollback restores.
        baseline=f"{MCS_MINIMUM_INDEX}..{MCS_MAXIMUM_INDEX}", unit="MCS-index",
        readback_leaf="maxDlMcs", value_kind="mcs-bounds", scope_kind="NRCellDU",
        scope_key=CELL_SCOPE_KEY, value_fields=("minDlMcs", "maxDlMcs"),
    ),
    ATTENUATION_ACTION_ID: SupplementaryAction(
        action_id=ATTENUATION_ACTION_ID, axis=ATTENUATION_AXIS,
        adapter=ATTENUATION_ADAPTER_KEY,
        policy_type_id=ATTENUATION_POLICY_TYPE_ID,
        readback_counter=ATTENUATION_CONFIGURATION_COUNTER,
        # Attenuation below maximum gain, so zero is full power and a larger
        # value means *less* downlink power.
        baseline=f"{ATTENUATION_MINIMUM_DB:.{ATTENUATION_DECIMALS}f}", unit="dB",
        readback_leaf="txAttenuationDb", value_kind="attenuation-db",
        scope_kind="NRCellDU", scope_key=CELL_SCOPE_KEY,
        value_fields=("txAttenuationDb",),
    ),
    SLICE_QUOTA_ACTION_ID: SupplementaryAction(
        action_id=SLICE_QUOTA_ACTION_ID, axis=SLICE_QUOTA_AXIS,
        adapter=SLICE_QUOTA_ADAPTER_KEY,
        policy_type_id=SLICE_QUOTA_POLICY_TYPE_ID,
        readback_counter=SLICE_QUOTA_CONFIGURATION_COUNTER,
        # No dedicated PRBs, the smallest admissible guarantee and the whole
        # carrier available: the RRM Policy Ratio List that constrains nothing.
        baseline=f"{SLICE_QUOTA_MINIMUM_PERCENT}:1:{SLICE_QUOTA_MAXIMUM_PERCENT}",
        unit="percent", readback_leaf="minPrbPolicyRatio", value_kind="prb-quota",
        scope_kind="S-NSSAI", scope_key=SLICE_SCOPE_KEY,
        value_fields=("dedicatedPrbPolicyRatio", "minPrbPolicyRatio",
                      "maxPrbPolicyRatio"),
    ),
})


def supplementary_action(action_id: str) -> SupplementaryAction:
    """The declaration for *action_id*, or refuse."""
    try:
        return SUPPLEMENTARY_ACTIONS[action_id]
    except KeyError:
        raise SupplementaryCapError(
            f"{action_id!r} is not a declared supplementary action; known: "
            + ", ".join(sorted(SUPPLEMENTARY_ACTIONS))
        ) from None


def supplementary_axis_declarations(
    action_ids: Sequence[str] = (CAP_ACTION_ID,)
) -> Tuple[Dict[str, Any], ...]:
    """Plain data for :class:`~assurance.vertical.VerticalDeployment`.

    The order is the apply order after the PRIMARY step, and reverse rollback
    unwinds it backwards.
    """
    return tuple(supplementary_action(item).declaration() for item in action_ids)


def _identity(contract_id: str) -> Dict[str, Any]:
    return dict(
        contract_id=contract_id, version="1.0.0",
        schema_version=ASSURANCE_SCHEMA_VERSION, document_status="NORMATIVE",
        standard_mapping={"e2sm-rc": "1.03", "e2sm-kpm": "2.03"},
    )


def _quantity(value: float, unit: str, source: str) -> TypedQuantity:
    return TypedQuantity(value, unit, Provenance.EXPERIMENT_CONFIG, source)


class SupplementaryCapRequest:
    """One controlled-UE cap, as the composition root asks for it.

    ``controlled_ue`` is the *identity* of a different, heavy, non-target UE in
    the A1 policy's own scope shape.  It is carried into the plan scope so the
    policy builder can resolve it at apply time; nothing here treats it as
    final, because an RNTI is cell-local and this identity is observed before
    the PRIMARY steering action runs.

    ``objective_throughput_floor_kbps`` and ``controlled_harm_reserve_kbps`` are
    deployment calibration values.  They are typed and frozen into the epoch by
    :func:`with_supplementary_cap`; this module does not invent them and refuses
    a request that does not carry them.
    """

    def __init__(
        self,
        *,
        controlled_ue: Mapping[str, Any],
        candidate_caps: Sequence[int],
        objective_throughput_floor_kbps: float,
        controlled_harm_reserve_kbps: float,
        calibration_ref: str,
        controlled_ue_scope_id: str,
    ) -> None:
        if not isinstance(controlled_ue, Mapping) or not controlled_ue:
            raise SupplementaryCapError(
                "a cap needs the identity of the controlled non-target UE")
        if not str(controlled_ue_scope_id or ""):
            raise SupplementaryCapError(
                "the controlled UE needs a scope id to charge its debt to")
        if objective_throughput_floor_kbps <= 0 or controlled_harm_reserve_kbps <= 0:
            raise SupplementaryCapError(
                "the objective floor and the controlled-UE harm reserve are "
                "positive calibration values, not defaults")
        if not str(calibration_ref or ""):
            raise SupplementaryCapError(
                "a certified harm bound names the calibration that produced it; "
                "an observed sample maximum alone is invalid")
        self.controlled_ue = dict(controlled_ue)
        self.candidate_caps = cap_candidate_values(candidate_caps)
        self.objective_throughput_floor_kbps = float(objective_throughput_floor_kbps)
        self.controlled_harm_reserve_kbps = float(controlled_harm_reserve_kbps)
        self.calibration_ref = str(calibration_ref)
        self.controlled_ue_scope_id = str(controlled_ue_scope_id)


def cap_candidate_values(values: Sequence[int]) -> Tuple[int, ...]:
    """The finite, de-duplicated, in-range cap catalog, ascending.

    Refuses an empty set -- a candidate axis with no members has undefined
    cardinality -- and anything outside the frozen APPLY range.  The uncapped
    sentinel is never a candidate: a cap is removed by withdrawing the policy
    and restoring the captured prior value, never by applying zero.
    """
    ordered = tuple(sorted({int(value) for value in values}))
    if not ordered:
        raise SupplementaryCapError("a finite cap catalog needs at least one value")
    outside = [value for value in ordered
               if not CAP_MINIMUM_PRB <= value <= CAP_MAXIMUM_PRB]
    if outside:
        raise SupplementaryCapError(
            f"cap candidates {outside} are outside the frozen "
            f"[{CAP_MINIMUM_PRB},{CAP_MAXIMUM_PRB}] PRB range")
    return ordered


def cap_axis_baseline() -> str:
    """The uncapped configuration the cap axis rests at.

    ``"0"`` is the gNB's uncapped sentinel: what a pre-policy readback reports
    for an unmanaged UE, what reverse rollback restores, and what the contracted
    safe state holds.
    """
    return SUPPLEMENTARY_ACTIONS[CAP_ACTION_ID].baseline


def _configuration_contracts(
    *, family: str, action: SupplementaryAction, scope_id: str,
    deployment: DeploymentBinding, cadence_ms: int, hold_ms: int,
) -> Tuple[CounterBinding, MeasurementContract]:
    counter_id = f"counter/{family}/{action.action_id}"
    measurement_id = f"measurement/{family}/{action.action_id}"
    # A UE cap is charged to the controlled UE; a cell-wide or slice-wide
    # configuration is charged to the cell or the S-NSSAI, under its own key,
    # so nothing can read a cell's MCS ceiling back as one UE's.
    scope_key = action.scope_key
    counter = CounterBinding(
        counter_id=counter_id,
        deployment_counter_name=action.readback_counter,
        source=MeasurementSource.CONFIGURATION_READBACK,
        scope_keys=(scope_key,),
        unit=action.unit,
        native_cadence_ms=cadence_ms,
        deployment_binding_ref=deployment.contract_id,
    )
    measurement = MeasurementContract(
        **_identity(measurement_id),
        counter_id=counter_id,
        scope_selector={scope_key: scope_id},
        membership_snapshot=(scope_id,),
        # The family's own evidence grid, not a second one: a window on a
        # different cadence cannot be read beside the objective and controlled
        # throughput windows it has to be judged with.
        cadence_ms=cadence_ms,
        window_width_ms=hold_ms,
        window_stride_ms=hold_ms,
        overlap=OverlapPolicy.DISJOINT,
        aggregation=Aggregation.MAX,
        estimator=Estimator.EMPIRICAL_QUANTILE,
        minimum_entity_count=1,
        hold_ms=hold_ms,
        gap_policy=GapPolicy.REJECT_WINDOW,
        missing_interval_charge=_quantity(1, action.unit, "action102/missing-config"),
        freshness_bound_ms=cadence_ms,
        clock_requirement=ClockRequirement.SYNCHRONISED_REQUIRED,
        # A configuration readback is exact: the scheduler either carries the
        # value or it does not, so an uncertainty band here would only be a way
        # to call a mismatch a match.
        uncertainty_rule=UncertaintyRule(
            "configuration-exact", _quantity(0, action.unit, "action102/readback")),
    )
    return counter, measurement


def _throughput_contracts(
    *, family: str, scope_key: str, scope_id: str, suffix: str,
    deployment: DeploymentBinding, hold_ms: int, cadence_ms: int,
) -> Tuple[CounterBinding, MeasurementContract]:
    counter_id = f"counter/{family}/ue-throughput-{suffix}"
    measurement_id = f"measurement/{family}/ue-throughput-{suffix}"
    counter = CounterBinding(
        counter_id=counter_id,
        deployment_counter_name=_THROUGHPUT_COUNTER,
        source=MeasurementSource.E2_KPM,
        scope_keys=(scope_key,),
        unit="kbit/s",
        native_cadence_ms=cadence_ms,
        deployment_binding_ref=deployment.contract_id,
    )
    measurement = MeasurementContract(
        **_identity(measurement_id),
        counter_id=counter_id,
        scope_selector={scope_key: scope_id},
        membership_snapshot=(scope_id,),
        cadence_ms=cadence_ms,
        window_width_ms=hold_ms,
        window_stride_ms=hold_ms,
        overlap=OverlapPolicy.DISJOINT,
        aggregation=Aggregation.MEAN,
        estimator=Estimator.SAMPLE_MEAN,
        minimum_entity_count=1,
        hold_ms=hold_ms,
        gap_policy=GapPolicy.CONSERVATIVE_CHARGE,
        missing_interval_charge=_quantity(5, "ms", "action102/missing-throughput"),
        freshness_bound_ms=cadence_ms,
        clock_requirement=ClockRequirement.SYNCHRONISED_REQUIRED,
        uncertainty_rule=UncertaintyRule(
            "bounded_absolute",
            _quantity(1, "kbit/s", "action102/kpm-f3-calibration")),
    )
    return counter, measurement


def with_supplementary_cap(bundle: Any, request: SupplementaryCapRequest) -> Any:
    """Return *bundle* extended with one SUPPLEMENTARY controlled-UE cap.

    Refuses a family the composition policy does not admit a live cap in, and
    refuses a bundle whose configuration surface already carries the cap axis --
    composing the same axis twice has no single reversal.
    """
    action = SUPPLEMENTARY_ACTIONS[CAP_ACTION_ID]
    family = str(getattr(bundle, "family", ""))
    if not live_cap_admissible(family):
        raise SupplementaryCapError(
            f"{family} may not carry a live {CAP_ACTION_ID}; the contract admits "
            f"one only in {', '.join(LIVE_CAP_FAMILIES)}")
    if action.axis in dict(bundle.baseline_config):
        raise SupplementaryCapError(
            "this bundle already carries the cap axis; one cap per composition")
    objective_scope_id = str(dict(bundle.scope).get("ueId") or "")
    if not objective_scope_id:
        raise SupplementaryCapError("the bundle names no objective UE to protect")
    if request.controlled_ue_scope_id == objective_scope_id:
        raise SupplementaryCapError(
            "the controlled UE is the objective UE; a cap must control a "
            "different, heavy, non-target UE")

    hold_ms = int(bundle.target.hold_ms)
    cadence_ms = max(int(item.cadence_ms) for item in bundle.measurements)
    cap_counter, cap_measurement = _configuration_contracts(
        family=family, action=action, scope_id=request.controlled_ue_scope_id,
        deployment=bundle.deployment, cadence_ms=cadence_ms, hold_ms=hold_ms)
    objective_counter, objective_measurement = _throughput_contracts(
        family=family, scope_key="ueId", scope_id=objective_scope_id,
        suffix="objective", deployment=bundle.deployment, hold_ms=hold_ms,
        cadence_ms=cadence_ms)
    controlled_counter, controlled_measurement = _throughput_contracts(
        family=family, scope_key="controlledUeId",
        scope_id=request.controlled_ue_scope_id, suffix="controlled",
        deployment=bundle.deployment, hold_ms=hold_ms, cadence_ms=cadence_ms)

    floor = TypedConstraint(
        objective_measurement.contract_id,
        ComparisonOperator.GREATER_OR_EQUAL,
        _quantity(request.objective_throughput_floor_kbps, "kbit/s",
                  request.calibration_ref),
    )
    floor_predicate = TargetPredicate(
        f"{family}/objective-ue-throughput-floor",
        floor,
        mandatory=True,
        description=(
            "the objective UE's delivered KPM Format 3 DRB.UEThpDl stays at or "
            "above its frozen floor for the whole hold, with a supplementary "
            "cap live on a different UE"),
    )
    floor_watchdog = WatchdogContract(
        **_identity(f"watchdog/{family}/objective-ue-throughput-floor"),
        watchdog_id=f"wd/{family}/objective-ue-throughput-floor",
        trigger=floor,
        action=WatchdogAction.STOP_AND_ROLLBACK,
        max_evaluation_latency_ms=1000,
    )
    controlled_watchdog = WatchdogContract(
        **_identity(f"watchdog/{family}/controlled-ue-cap"),
        watchdog_id=f"wd/{family}/controlled-ue-cap",
        trigger=TypedConstraint(
            controlled_measurement.contract_id,
            ComparisonOperator.GREATER_OR_EQUAL,
            _quantity(request.controlled_harm_reserve_kbps, "kbit/s",
                      request.calibration_ref),
        ),
        action=WatchdogAction.STOP_AND_ROLLBACK,
        max_evaluation_latency_ms=1000,
    )
    controlled_scope = {"controlledUeId": request.controlled_ue_scope_id}
    controlled_bound = CertifiedHarmBound(
        f"bound/{family}/controlled-ue-cap",
        _quantity(request.controlled_harm_reserve_kbps, "kbit/s",
                  f"{request.calibration_ref}#calibration"),
        _quantity(request.controlled_harm_reserve_kbps * 0.1, "kbit/s",
                  f"{request.calibration_ref}#margin"),
        _quantity(request.controlled_harm_reserve_kbps * 1.1, "kbit/s",
                  f"{request.calibration_ref}#admission"),
        f"{controlled_measurement.contract_id}#uncertainty",
        controlled_scope,
        int(bundle.harm.bounds[0].enforced_timeout_ms) if bundle.harm.bounds else 10000,
        (request.calibration_ref,),
        f"proof/{family}/controlled-ue-cap",
    )

    option = bundle.target.options[0]
    capped_option = replace(
        option,
        parameter_space={
            **{key: tuple(values) for key, values in option.parameter_space.items()},
            action.axis: tuple(str(value) for value in request.candidate_caps),
        },
    )
    target = replace(
        bundle.target,
        predicates=tuple(bundle.target.predicates) + (floor_predicate,),
        options=(capped_option,) + tuple(bundle.target.options[1:]),
    )
    harm = replace(
        bundle.harm,
        bounds=tuple(bundle.harm.bounds) + (controlled_bound,),
        watchdogs=tuple(bundle.harm.watchdogs) + (floor_watchdog, controlled_watchdog),
    )
    scope = dict(bundle.scope)
    scope[CONTROLLED_UE_SCOPE_KEY] = dict(request.controlled_ue)
    scope["controlledUeId"] = request.controlled_ue_scope_id
    return replace(
        bundle,
        counters=tuple(bundle.counters) + (
            cap_counter, objective_counter, controlled_counter),
        measurements=tuple(bundle.measurements) + (
            cap_measurement, objective_measurement, controlled_measurement),
        target=target,
        watchdogs=tuple(bundle.watchdogs) + (floor_watchdog, controlled_watchdog),
        harm=harm,
        baseline_config={**dict(bundle.baseline_config), action.axis: action.baseline},
        safe_state={**dict(bundle.safe_state), action.axis: action.baseline},
        scope=scope,
    )
