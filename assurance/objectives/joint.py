"""``JointIntentSet`` -- one Kernel case over *any number* of intents and actions.

The families in this package each describe one intent on one UE, with one
frozen candidate axis.  The Agent's board needs the opposite shape: one
``TargetContract`` that carries **every** admitted intent's predicates, keyed
by intent, and one ``TargetOption`` whose parameter space is the product of
**every** action axis the deployment can move -- so one epoch freezes a
catalog the search can walk, and one trial judges every intent at once.

This module composes that from the family modules themselves.  Nothing is
re-stated: each intent's counters, measurements, predicates, watchdogs and
harm bounds are the ones its family module builds for its own UE, renamed so
that ``n`` intents can share an epoch without an id collision:

* measurement and counter ids gain ``@<intent>`` / ``@<ue>`` suffixes;
* predicate ids become ``<intent>/<predicate>``, which is what lets
  :func:`assurance.coordination.board.intent_states_from_verdicts` read the
  Kernel's per-predicate verdicts back into per-intent states;
* every action axis is scoped: ``servingCell@<ue>``, ``dlPrbCap@<ue>``,
  ``pfWeight@<ue>`` on a UE, ``dlMcsBounds@<cell>`` and
  ``txAttenuationDb@<cell>`` on a cell, ``slicePrbQuota@<sst>`` on an
  S-NSSAI.  A scoped axis is what lets two intents on two UEs both steer, and
  what lets the Write Gateway route each axis to the adapter bound to that
  scope -- which is why the scope is part of the axis and not a field beside
  it.

All predicates stay mandatory, so a trial's ``SUCCESS`` means every intent
was met in the same hold and validity region (task section 8's rule for
composite objectives, applied to the whole set).  A trial that meets some
intents and not others is a settled ``FAIL`` with per-predicate verdicts --
which is exactly the primed row the board records -- and is rolled back like
any other non-success.

The number of intents and the number of axes are read from the arguments.
Nothing here counts to three.

The catalog is the **product** of the exposed axes, so widening the axes
multiplies: every UE with a cap and a weight, every advertised cell with MCS
bounds and an attenuation, every S-NSSAI with a quota is 248832 candidates on
a two-UE two-cell deployment.  :func:`compose_joint` therefore states a
ceiling and refuses over it by name, before a single contract is built -- see
:data:`DEFAULT_MAX_CATALOG_CARDINALITY`.
"""

from __future__ import annotations
from assurance.contracts.actuation_request import enforced_timeout_ms_default

import re
from dataclasses import dataclass, field, replace
from typing import (
    Any, ClassVar, Dict, List, Mapping, Optional, Sequence, Tuple,
)

from assurance.actions.catalog import UE_DL_PRB_CAP_UNCAPPED_SENTINEL
from assurance.contracts.capability import (
    ActuatorBinding, ActuatorPath, CapabilityManifest, CompositionManifest,
    DeploymentBinding,
)
from assurance.contracts.catalog import CoordinationCasePolicy
from assurance.contracts.harm import (
    CertifiedHarmBound, HarmContract, HarmKind, WatchdogAction, WatchdogContract,
)
from assurance.contracts.measurement import CounterBinding, MeasurementContract
from assurance.contracts.target import (
    ComparisonOperator, TargetContract, TargetOption, TargetPredicate,
    TargetReleasePolicy, TargetVector, TypedConstraint,
)
from assurance.core.envelopes import ASSURANCE_SCHEMA_VERSION
from assurance.core.provenance import Provenance, TypedQuantity
from assurance.objectives.action102_support import (
    ATTENUATION_ACTION_ID, CAP_ACTION_ID, MCS_ACTION_ID, PRIORITY_ACTION_ID,
    SLICE_QUOTA_ACTION_ID, SUPPLEMENTARY_ACTIONS, SupplementaryCapError,
    _configuration_contracts, _throughput_contracts, cap_candidate_values,
)
from assurance.objectives.family import ObjectiveContractBundle
from assurance.objectives.ue_level_target import NCI_UNIT

__all__ = [
    "DEFAULT_ATTENUATION_LADDER",
    "DEFAULT_CAP_LADDER",
    "DEFAULT_MAX_CATALOG_CARDINALITY",
    "DEFAULT_MCS_LADDER",
    "DEFAULT_PF_LADDER",
    "DEFAULT_SLICE_QUOTA_LADDER",
    "INTENT_KINDS",
    "INTENT_KIND_KPI",
    "INTENT_KIND_SERVING_CELL",
    "JOINT_FAMILY",
    "CapAxisSpec",
    "CatalogTooLargeError",
    "IntentSpec",
    "JointComposition",
    "JointCompositionError",
    "McsBoundsAxisSpec",
    "PriorityAxisSpec",
    "SlicePrbQuotaAxisSpec",
    "SteeringAxisSpec",
    "STEERING_POLICY_TYPE_ID",
    "TxAttenuationAxisSpec",
    "catalog_cardinality",
    "compose_joint",
    "steering_axis",
    "cap_axis",
    "priority_axis",
    "mcs_bounds_axis",
    "slice_quota_axis",
    "tx_attenuation_axis",
]

JOINT_FAMILY = "JointIntentSet"
STEERING_POLICY_TYPE_ID = "AIC_UECellSteering_1.0.0"

#: Families whose bundle this composition knows how to fold in: the ones that
#: state their intent over a UE's serving cell (and, for the QoS ones, its
#: delivered quality).  A family outside this set is refused by name.
FOLDABLE_FAMILIES: Tuple[str, ...] = (
    "TrafficSteeringPreference", "UELevelTarget", "QoSTarget", "QoSandTSP",
)


class JointCompositionError(ValueError):
    """The intent set or action set cannot be composed into one case."""


class CatalogTooLargeError(JointCompositionError):
    """The exposed axes span more candidates than the sitting may freeze."""


def _identity(contract_id: str) -> Dict[str, Any]:
    return dict(
        contract_id=contract_id, version="1.0.0",
        schema_version=ASSURANCE_SCHEMA_VERSION, document_status="NORMATIVE",
        standard_mapping={"a1p": "2", "e2sm-rc": "1.03", "e2sm-kpm": "2.03"},
    )


def _quantity(value: float, unit: str, source: str) -> TypedQuantity:
    return TypedQuantity(value, unit, Provenance.EXPERIMENT_CONFIG, source)


def steering_axis(ue_id: str) -> str:
    return f"servingCell@{ue_id}"


def cap_axis(ue_id: str) -> str:
    return f"{SUPPLEMENTARY_ACTIONS[CAP_ACTION_ID].axis}@{ue_id}"


def priority_axis(ue_id: str) -> str:
    return f"{SUPPLEMENTARY_ACTIONS[PRIORITY_ACTION_ID].axis}@{ue_id}"


def mcs_bounds_axis(cell_nci: Any) -> str:
    return f"{SUPPLEMENTARY_ACTIONS[MCS_ACTION_ID].axis}@{int(cell_nci)}"


def tx_attenuation_axis(cell_nci: Any) -> str:
    return f"{SUPPLEMENTARY_ACTIONS[ATTENUATION_ACTION_ID].axis}@{int(cell_nci)}"


def slice_quota_axis(sst: Any) -> str:
    return f"{SUPPLEMENTARY_ACTIONS[SLICE_QUOTA_ACTION_ID].axis}@{int(sst)}"


#: The default ladder each axis kind exposes when the operator names none
#: (contract v3 section 3).  They are stated here, once, so the executor, the
#: Cockpit and the runner offer the same rungs rather than three opinions.
#: The cap and the weight ladders include their own neutral value, which the
#: specs recognise and re-add as the baseline rather than as a candidate.
DEFAULT_CAP_LADDER: Tuple[int, ...] = (0, 18, 12, 6)
DEFAULT_PF_LADDER: Tuple[float, ...] = (0.5, 1.0, 2.0, 4.0)
DEFAULT_MCS_LADDER: Tuple[str, ...] = ("0..28", "0..16", "10..28")
DEFAULT_ATTENUATION_LADDER: Tuple[str, ...] = ("0.0", "6.0", "12.0")
DEFAULT_SLICE_QUOTA_LADDER: Tuple[str, ...] = ("0:1:100", "0:1:60", "0:1:30")

#: How many frozen candidates one sitting may state.  The catalog is the
#: product of the exposed axes, so widening the axes multiplies rather than
#: adds: two UEs and two cells with every axis on is 248832 combinations.
#: Over this ceiling :func:`compose_joint` refuses **before anything is
#: written**, naming each axis and its size, so the operator narrows a ladder
#: or drops an axis deliberately.  A silent truncation would be worse than a
#: refusal: the search would report exhaustion over a catalog nobody chose.
#:
#: 4096 is where the measurement puts it.  Composition itself is free (1-3 ms
#: at every size); the cost is the epoch freeze, which canonicalises and
#: hashes every candidate and runs linear at roughly 2.5 ms and 2.5 kB each:
#: 512 candidates freeze in 1.6 s / 1.7 MB, 2048 in 4.8 s / 4.8 MB, 4096 in
#: 10.3 s / 10.2 MB.  Ten seconds once, against a sitting that then spends
#: K trials of a 30 s hold, is worth paying; 16384 would be forty, which the
#: operator would feel and would not have chosen.
#:
#: 2026-09-19 (오너: "셀이 10개 되면 선택이 불가하냐"): the epoch now freezes the
#: *domain* -- allowed values per axis -- not an enumeration, so the freeze no
#: longer grows with the product (``generate_catalog``).  The ceiling stays as
#: an operator guard against a mistyped ladder, far above any real sitting.
DEFAULT_MAX_CATALOG_CARDINALITY = 1_000_000_000

#: What each axis kind is called in the refusal message, in exposure order.
_AXIS_KIND_LABELS: Tuple[Tuple[str, str], ...] = (
    ("servingCell", "steer"),
    (SUPPLEMENTARY_ACTIONS[CAP_ACTION_ID].axis, "cap"),
    (SUPPLEMENTARY_ACTIONS[PRIORITY_ACTION_ID].axis, "pf"),
    (SUPPLEMENTARY_ACTIONS[MCS_ACTION_ID].axis, "mcs"),
    (SUPPLEMENTARY_ACTIONS[ATTENUATION_ACTION_ID].axis, "atten"),
    (SUPPLEMENTARY_ACTIONS[SLICE_QUOTA_ACTION_ID].axis, "quota"),
)


#: The E2SM-RC control each non-UE action is carried by, as the Campaign 5
#: producer already advertises it (``oran/campaign5/families.py``) and, for the
#: slice quota, as the RRM Policy Ratio List the Style 2 / Action 6 encoder
#: fork writes.
_NON_UE_SERVICE_MODELS: Mapping[str, Mapping[str, str]] = {
    MCS_ACTION_ID: {"serviceModel": "E2SM-RC", "style": "2", "action": "101",
                    "profile": "E2SM-RC-STYLE2-ACTION101"},
    ATTENUATION_ACTION_ID: {"serviceModel": "E2SM-RC", "style": "2",
                            "action": "104",
                            "profile": "E2SM-RC-STYLE2-ACTION104"},
    SLICE_QUOTA_ACTION_ID: {"serviceModel": "E2SM-RC", "style": "2",
                            "action": "6",
                            "profile": "RRM-POLICY-RATIO-LIST"},
}


#: An S-NSSAI SD, as the slice actuator's own model spells it.
_SD_HEX = re.compile(r"[0-9A-Fa-f]{6}")


def _axis_kind(axis: str) -> str:
    return str(axis).split("@", 1)[0]


def _family_tag(spec: Any) -> str:
    """The contract-id prefix one non-UE axis's readback lives under.

    The scope, not the UE: ``joint@cell-12345678`` and ``joint@slice-1``, so a
    cell's and a slice's readbacks can never collide with a UE's and neither
    can be mistaken for the other by position.
    """
    return "joint@" + str(spec.scope).replace("@", "-")


def _readback_scope_id(spec: Any) -> str:
    """What the configuration readback of one non-UE axis is keyed by."""
    return str(getattr(spec, "scope_id", None) or spec.scope.split("@", 1)[1])


def catalog_cardinality(specs: Sequence[Any]) -> int:
    """How many candidates the product of these axes spans."""
    total = 1
    for spec in specs:
        total *= max(1, len(tuple(spec.values)))
    return total


def _cardinality_by_kind(specs: Sequence[Any]) -> List[Tuple[str, int]]:
    """``[("steer", 4), ("cap", 16), ...]`` -- the product, factored by kind.

    A refusal that only stated the total would leave the operator guessing
    which ladder to narrow, so the message names each kind and how much it
    multiplies by.
    """
    sizes: Dict[str, int] = {}
    for spec in specs:
        kind = _axis_kind(spec.axis)
        sizes[kind] = sizes.get(kind, 1) * max(1, len(tuple(spec.values)))
    labels = dict(_AXIS_KIND_LABELS)
    order = {kind: index for index, (kind, _label) in enumerate(_AXIS_KIND_LABELS)}
    return [(labels.get(kind, kind), size) for kind, size in
            sorted(sizes.items(), key=lambda item: (order.get(item[0], 99), item[0]))]


def _refuse_oversized_catalog(specs: Sequence[Any], ceiling: int) -> None:
    """Refuse a sitting whose axes span more than it may freeze.

    Called before a single contract is built, let alone written: the operator
    narrows a ladder or drops an axis kind, and nothing about the deployment
    has changed in the meantime.
    """
    ceiling = int(ceiling)
    if ceiling <= 0:
        raise JointCompositionError(
            "the catalog ceiling is a positive number of candidates")
    total = catalog_cardinality(specs)
    if total <= ceiling:
        return
    factors = " x ".join(f"{label} {size}" for label, size in
                         _cardinality_by_kind(specs))
    raise CatalogTooLargeError(
        f"this sitting exposes {total} combinations ({factors}); the ceiling "
        f"is {ceiling} -- narrow a ladder or drop an axis with --axes")


#: An intent that names a serving cell: the folded predicate pins that cell.
INTENT_KIND_SERVING_CELL = "serving-cell"
#: An intent whose condition is a KPI, not a cell.  Its folded serving-cell
#: predicate becomes "still served by one of these cells" -- see
#: :class:`IntentSpec`.
INTENT_KIND_KPI = "kpi"
INTENT_KINDS: Tuple[str, ...] = (INTENT_KIND_SERVING_CELL, INTENT_KIND_KPI)


@dataclass(frozen=True)
class IntentSpec:
    """One admitted intent: which family, on which UE, naming which cell.

    ``kind`` says what the intent's condition really is, and the fold follows.

    * ``serving-cell`` (the default) is what this console has always composed:
      the operator named a cell, so the folded predicate pins it -- the UE's
      serving cell **equals** ``target_nci`` at every sample of the hold.
    * ``kpi`` is the intent whose condition is a measured quantity (a goodput
      floor), not a cell.  Such an intent names no cell, and pinning one would
      make the Kernel contradict the executor: a control that meets the KPI by
      steering the UE elsewhere would settle ``SETTLED_NON_SUCCESS`` and be
      rolled back, so the configuration that met the intent could never stay
      applied.  The folded serving-cell predicate therefore becomes membership
      -- the UE is **served by one of** ``served_cells`` throughout -- which is
      the honest Kernel-side statement of "this UE stayed attached to this
      deployment while its KPI was measured".  The KPI itself is judged by the
      executor against the owner's target vector, over measured counters, and
      that judgement is not this contract's business.

    ``served_cells`` are the cells the deployment advertises.  Left empty on a
    ``kpi`` intent it falls back to the two cells this intent already names, so
    the membership set is never empty (which the Kernel refuses).
    """

    intent_id: str
    family: str
    ue_id: str
    home_nci: int
    target_nci: int
    sentence: str = ""
    cell_id: str = "NRCellDU-1"
    kind: str = INTENT_KIND_SERVING_CELL
    served_cells: Tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.intent_id).strip():
            raise JointCompositionError("an intent needs an id")
        if "/" in self.intent_id or "@" in self.intent_id:
            raise JointCompositionError(
                f"intent id {self.intent_id!r} may not contain '/' or '@'; both are "
                "separators in the renamed contract ids")
        if self.family not in FOLDABLE_FAMILIES:
            raise JointCompositionError(
                f"{self.family} cannot be folded into a joint case; foldable: "
                + ", ".join(FOLDABLE_FAMILIES))
        if self.kind not in INTENT_KINDS:
            raise JointCompositionError(
                f"intent {self.intent_id}: kind {self.kind!r} is not one of "
                + ", ".join(INTENT_KINDS))
        object.__setattr__(self, "ue_id", str(self.ue_id))
        cells = tuple(sorted({int(item) for item in self.served_cells}))
        if not cells:
            cells = tuple(sorted({int(self.home_nci), int(self.target_nci)}))
        object.__setattr__(self, "served_cells", cells)

    @property
    def membership_values(self) -> Tuple[str, ...]:
        """The strings a ``MEMBER_OF`` constraint admits for this intent.

        The Kernel compares ``str(observed)`` against this set, and ``observed``
        is the float aggregate of the NCI counter, so ``87654321`` reaches the
        comparison as ``"87654321.0"``.  Both spellings are admitted rather
        than assuming which one the aggregation produces.
        """
        return tuple(form for cell in self.served_cells
                     for form in (str(int(cell)), str(float(cell))))

    @property
    def scope(self) -> Dict[str, Any]:
        return {
            "ueId": self.ue_id,
            "cellId": str(self.cell_id),
            "targetServingCell": str(int(self.target_nci)),
            "homeServingCell": str(int(self.home_nci)),
        }


@dataclass(frozen=True)
class SteeringAxisSpec:
    """The steering action on one UE: the cells it may be put on."""

    ue_id: str
    baseline_nci: int
    cells: Tuple[int, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "ue_id", str(self.ue_id))
        cells = tuple(sorted({int(item) for item in self.cells} | {int(self.baseline_nci)}))
        object.__setattr__(self, "cells", cells)

    @property
    def axis(self) -> str:
        return steering_axis(self.ue_id)

    @property
    def action_id(self) -> str:
        return f"steer@{self.ue_id}"

    @property
    def scope(self) -> str:
        return f"ue@{self.ue_id}"

    @property
    def unit(self) -> str:
        return NCI_UNIT

    @property
    def baseline(self) -> str:
        return str(int(self.baseline_nci))

    @property
    def values(self) -> Tuple[str, ...]:
        return tuple(str(item) for item in self.cells)


@dataclass(frozen=True)
class CapAxisSpec:
    """The DL PRB cap on one UE: the cap values it may take, plus uncapped."""

    ue_id: str
    cell_nci: int
    caps: Tuple[int, ...]
    harm_reserve_kbps: float
    calibration_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "ue_id", str(self.ue_id))
        # The uncapped sentinel is the axis's baseline, never one of its
        # candidates, so a ladder that names it (the default one does) keeps
        # it -- as the baseline ``values`` already prepends -- instead of
        # being refused for stating the value the axis rests at.
        object.__setattr__(self, "caps", cap_candidate_values(
            [item for item in self.caps
             if int(item) != UE_DL_PRB_CAP_UNCAPPED_SENTINEL]))
        if self.harm_reserve_kbps <= 0 or not str(self.calibration_ref or ""):
            raise JointCompositionError(
                "a cap axis needs a positive controlled-UE harm reserve and the "
                "calibration that produced it")

    @property
    def axis(self) -> str:
        return cap_axis(self.ue_id)

    @property
    def action_id(self) -> str:
        return f"cap@{self.ue_id}"

    @property
    def scope(self) -> str:
        return f"ue@{self.ue_id}"

    @property
    def unit(self) -> str:
        return SUPPLEMENTARY_ACTIONS[CAP_ACTION_ID].unit

    @property
    def baseline(self) -> str:
        return SUPPLEMENTARY_ACTIONS[CAP_ACTION_ID].baseline

    @property
    def values(self) -> Tuple[str, ...]:
        return (self.baseline,) + tuple(str(item) for item in self.caps)


@dataclass(frozen=True)
class PriorityAxisSpec:
    """The scheduler weight on one UE: the weights it may take, plus neutral."""

    ue_id: str
    cell_nci: int
    weights: Tuple[float, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "ue_id", str(self.ue_id))
        neutral = float(SUPPLEMENTARY_ACTIONS[PRIORITY_ACTION_ID].baseline)
        weights = tuple(sorted({float(item) for item in self.weights} - {neutral}))
        if not weights:
            raise JointCompositionError("a priority axis needs at least one non-neutral weight")
        object.__setattr__(self, "weights", weights)

    @property
    def axis(self) -> str:
        return priority_axis(self.ue_id)

    @property
    def action_id(self) -> str:
        return f"priority@{self.ue_id}"

    @property
    def scope(self) -> str:
        return f"ue@{self.ue_id}"

    @property
    def unit(self) -> str:
        return SUPPLEMENTARY_ACTIONS[PRIORITY_ACTION_ID].unit

    @property
    def baseline(self) -> str:
        return SUPPLEMENTARY_ACTIONS[PRIORITY_ACTION_ID].baseline

    @property
    def values(self) -> Tuple[str, ...]:
        return (self.baseline,) + tuple(str(item) for item in self.weights)


class _CompositeAxisSpec:
    """Shared shape for the axes whose value is one composite string.

    The three cell- and slice-scoped axes differ only in what they are scoped
    to and which ladder they carry; everything else -- validating each rung
    against the declaration that owns it, dropping the baseline out of the
    candidates and putting it back at the front -- is the same, and stating it
    once is what keeps their ordering and their refusals identical.

    A plain base class, not a dataclass: the subclasses stay ordinary frozen
    dataclasses whose first positional field is the thing they are scoped to.
    """

    #: The supplementary action the subclass declares.
    action_key: ClassVar[str] = ""

    @property
    def rungs(self) -> Tuple[str, ...]:
        """The candidates, baseline excluded, in the ladder's own order."""
        raise NotImplementedError

    def _checked(self, rungs: Sequence[Any]) -> Tuple[str, ...]:
        declared = SUPPLEMENTARY_ACTIONS[self.action_key]
        ordered: List[str] = []
        for item in rungs:
            try:
                # Canonicalise through the declaration so two spellings of the
                # same configuration cannot both sit in one frozen catalog.
                text = declared.axis_value_text(declared.policy_values(item))
            except SupplementaryCapError as exc:
                raise JointCompositionError(
                    f"{declared.action_id} ladder rung {item!r}: {exc}") from None
            if text != declared.baseline and text not in ordered:
                ordered.append(text)
        if not ordered:
            raise JointCompositionError(
                f"a {declared.action_id} axis needs at least one rung other "
                f"than its baseline {declared.baseline!r}")
        return tuple(ordered)

    @property
    def action(self) -> Any:
        return SUPPLEMENTARY_ACTIONS[self.action_key]

    @property
    def unit(self) -> str:
        return self.action.unit

    @property
    def baseline(self) -> str:
        """이 축의 기준값 — 선언 액션의 상수, **또는 배포가 실제로 있는 값**.

        2026-09-18 (오너 결정): 선언 상수만 쓰면 배포가 그 값에 있지 않을 때
        허가증이 라이브와 어긋나 `REJECTED_CONFIG_MISMATCH` 로 **모든 시행이 쓰기 전에
        거절된다**.  실측: gnb2(NCI 87654321)는 배포 시점부터 10 dB 인데 선언 상수는
        `0.0` 이라, 전력 축을 켠 뒤 완주 판 5 개 중 3 개가 그렇게 죽었다 —
        조종이든 캡이든 무관하게, 그 셀이 설정에 들어 있다는 것만으로.

        되읽기는 이미 "그 셀이 지금 있는 값" 을 본다.  기준값도 같은 뜻이어야 한다.
        `observed_baseline` 이 주어지면 그것을 쓰고, 없으면 종전의 선언 상수를 쓴다.
        rung 이 아니라는 성질은 그대로다 -- `values` 는 기준값을 맨 앞에 두고
        사다리를 잇는다.
        """
        stated = getattr(self, "observed_baseline", None)
        return self.action.baseline if stated in (None, "") else str(stated)

    @property
    def values(self) -> Tuple[str, ...]:
        return (self.baseline,) + tuple(self.rungs)


@dataclass(frozen=True)
class McsBoundsAxisSpec(_CompositeAxisSpec):
    """The downlink MCS floor and ceiling on one cell.

    One axis, not two: ``ci mcs`` and the ``AIC_DlMcsBounds_1.0.0`` policy
    each carry both bounds, so splitting them would mean two writes and an
    intermediate link-adaptation window nobody asked for.
    """

    cell_nci: int
    bounds: Tuple[str, ...] = DEFAULT_MCS_LADDER

    action_key: ClassVar[str] = MCS_ACTION_ID

    def __post_init__(self) -> None:
        object.__setattr__(self, "cell_nci", int(self.cell_nci))
        object.__setattr__(self, "bounds", self._checked(self.bounds))

    @property
    def rungs(self) -> Tuple[str, ...]:
        return self.bounds

    @property
    def axis(self) -> str:
        return mcs_bounds_axis(self.cell_nci)

    @property
    def action_id(self) -> str:
        return f"mcs@{self.cell_nci}"

    @property
    def scope(self) -> str:
        return f"cell@{self.cell_nci}"


@dataclass(frozen=True)
class TxAttenuationAxisSpec(_CompositeAxisSpec):
    """The downlink transmit attenuation on one cell, in dB below full gain.

    A larger value is *less* power, which is the direction the manifest and
    the ``rfatt`` handler both record; the ladder is stated in the knob's own
    one-decimal resolution so every rung round trips.
    """

    cell_nci: int
    attenuations: Tuple[str, ...] = DEFAULT_ATTENUATION_LADDER
    #: 이 셀이 **실제로 있는** 감쇠.  주어지면 기준값이 되고, 없으면 선언 상수를 쓴다.
    observed_baseline: Optional[str] = None

    action_key: ClassVar[str] = ATTENUATION_ACTION_ID

    def __post_init__(self) -> None:
        object.__setattr__(self, "cell_nci", int(self.cell_nci))
        object.__setattr__(self, "attenuations", self._checked(self.attenuations))

    @property
    def rungs(self) -> Tuple[str, ...]:
        return self.attenuations

    @property
    def axis(self) -> str:
        return tx_attenuation_axis(self.cell_nci)

    @property
    def action_id(self) -> str:
        return f"atten@{self.cell_nci}"

    @property
    def scope(self) -> str:
        return f"cell@{self.cell_nci}"


@dataclass(frozen=True)
class SlicePrbQuotaAxisSpec(_CompositeAxisSpec):
    """The RRM Policy Ratio List on one S-NSSAI, as ``dedicated:min:max``.

    The SD travels on the scope, not on the axis: two slices with the same SST
    are two axes, and the axis value stays the three ratios the policy writes
    together.
    """

    sst: int
    sd: str = ""
    quotas: Tuple[str, ...] = DEFAULT_SLICE_QUOTA_LADDER

    action_key: ClassVar[str] = SLICE_QUOTA_ACTION_ID

    def __post_init__(self) -> None:
        sst = int(self.sst)
        if not 1 <= sst <= 255:
            raise JointCompositionError(
                f"an S-NSSAI SST is 1..255, got {self.sst!r}")
        object.__setattr__(self, "sst", sst)
        sd = str(self.sd or "")
        if sd and _SD_HEX.fullmatch(sd) is None:
            # The same shape ``oran/slice_actuator/model.py`` enforces: an SD
            # is six hexadecimal digits, and one that is not would name a
            # slice the producer cannot address.
            raise JointCompositionError(
                f"an S-NSSAI SD is six hexadecimal digits, got {self.sd!r}")
        object.__setattr__(self, "sd", sd.upper() if sd else "")
        object.__setattr__(self, "quotas", self._checked(self.quotas))

    @property
    def rungs(self) -> Tuple[str, ...]:
        return self.quotas

    @property
    def axis(self) -> str:
        return slice_quota_axis(self.sst)

    @property
    def action_id(self) -> str:
        return f"quota@{self.sst}"

    @property
    def scope(self) -> str:
        return f"slice@{self.sst}"

    @property
    def scope_id(self) -> str:
        """What the readback is keyed by: the S-NSSAI, SD included."""
        return f"{self.sst}-{self.sd}" if self.sd else str(self.sst)


@dataclass(frozen=True)
class JointComposition:
    """The composed bundle plus the maps the board and the runtime read."""

    bundle: ObjectiveContractBundle
    intents: Tuple[IntentSpec, ...]
    steering_axes: Tuple[SteeringAxisSpec, ...]
    cap_axes: Tuple[CapAxisSpec, ...]
    priority_axes: Tuple[PriorityAxisSpec, ...]
    #: intent id -> the (renamed) predicate ids that judge it.
    intent_predicates: Mapping[str, Tuple[str, ...]]
    #: action id -> configuration axis.
    action_axes: Mapping[str, str]
    #: axis -> baseline value.
    axis_baselines: Mapping[str, str]
    #: steering UE -> (min, max) serving-cell measurement contracts, for the
    #: policy builder that needs them.
    steering_measurements: Mapping[str, Tuple[MeasurementContract, MeasurementContract]]
    #: axis -> the actuator binding that carries it.
    actuators_by_axis: Mapping[str, ActuatorBinding]
    #: The cell- and slice-scoped axes.  They come last and default to empty
    #: so every caller that composes the three UE-scoped axes keeps working
    #: unchanged.
    mcs_axes: Tuple[McsBoundsAxisSpec, ...] = ()
    attenuation_axes: Tuple[TxAttenuationAxisSpec, ...] = ()
    slice_quota_axes: Tuple[SlicePrbQuotaAxisSpec, ...] = ()
    #: axis -> the scope it is written at (``ue@131``, ``cell@12345678``,
    #: ``slice@1``).  What the executor needs to route a non-UE axis.
    axis_scopes: Mapping[str, str] = field(default_factory=dict)

    @property
    def axis_specs(self) -> Tuple[Any, ...]:
        """Every declared axis spec, in exposure order."""
        return (*self.steering_axes, *self.cap_axes, *self.priority_axes,
                *self.mcs_axes, *self.attenuation_axes, *self.slice_quota_axes)

    @property
    def catalog_cardinality(self) -> int:
        """How many candidates the frozen catalog holds."""
        return catalog_cardinality(self.axis_specs)

    @property
    def intent_ids(self) -> Tuple[str, ...]:
        return tuple(item.intent_id for item in self.intents)

    @property
    def action_ids(self) -> Tuple[str, ...]:
        return tuple(self.action_axes)

    def describe(self) -> Dict[str, Any]:
        return {
            "family": JOINT_FAMILY,
            "intents": [{"intentId": i.intent_id, "family": i.family, "ueId": i.ue_id,
                         "homeCell": int(i.home_nci), "targetCell": int(i.target_nci),
                         "sentence": i.sentence,
                         "predicates": list(self.intent_predicates.get(i.intent_id, ()))}
                        for i in self.intents],
            "actions": [{"actionId": action_id, "axis": axis,
                         "baseline": self.axis_baselines[axis]}
                        for action_id, axis in self.action_axes.items()],
            "parameterSpace": {
                key: list(values)
                for key, values in self.bundle.target.options[0].parameter_space.items()},
            "axisScopes": dict(self.axis_scopes),
            "catalogCardinality": self.catalog_cardinality,
            "holdMs": int(self.bundle.target.hold_ms),
            "maxTrials": int(self.bundle.case_policy.max_trials),
        }


# --------------------------------------------------------------------------- #
# folding one intent's family bundle
# --------------------------------------------------------------------------- #


def _family_module(family: str) -> Any:
    from assurance.objectives import FAMILY_MODULES

    return FAMILY_MODULES[family]()


def _rename_ref(ref: str, mapping: Mapping[str, str]) -> str:
    """Rename a contract reference, honouring the ``#fragment`` convention."""
    base, separator, fragment = str(ref).partition("#")
    renamed = mapping.get(base, base)
    return renamed + separator + fragment


@dataclass
class _Folded:
    counters: List[CounterBinding] = field(default_factory=list)
    measurements: List[MeasurementContract] = field(default_factory=list)
    predicates: List[TargetPredicate] = field(default_factory=list)
    watchdogs: List[WatchdogContract] = field(default_factory=list)
    bounds: List[CertifiedHarmBound] = field(default_factory=list)
    reserve_ms: float = 0.0
    missing_charge_ms: float = 0.0
    hold_ms: int = 0


def _fold_intent(spec: IntentSpec, deployment: DeploymentBinding,
                 folded: _Folded, intent_predicates: Dict[str, Tuple[str, ...]],
                 steering_measurements: Dict[str, Tuple[MeasurementContract, MeasurementContract]],
                 ) -> None:
    module = _family_module(spec.family)
    bundle = module.contract_bundle(scope=spec.scope, deployment_binding=deployment)
    tag = spec.intent_id
    counter_names = {c.counter_id: f"{c.counter_id}@{spec.ue_id}" for c in bundle.counters}
    measurement_names = {m.contract_id: f"{m.contract_id}@{tag}" for m in bundle.measurements}

    for counter in bundle.counters:
        renamed = replace(counter, counter_id=counter_names[counter.counter_id])
        if all(existing.counter_id != renamed.counter_id for existing in folded.counters):
            folded.counters.append(renamed)
    renamed_measurements: List[MeasurementContract] = []
    for measurement in bundle.measurements:
        renamed_measurements.append(replace(
            measurement,
            contract_id=measurement_names[measurement.contract_id],
            counter_id=counter_names[measurement.counter_id],
        ))
    folded.measurements.extend(renamed_measurements)
    predicate_ids: List[str] = []
    for predicate in bundle.target.predicates:
        constraint = replace(
            predicate.constraint,
            measurement_ref=_rename_ref(predicate.constraint.measurement_ref,
                                        measurement_names))
        description = predicate.description
        if spec.kind == INTENT_KIND_KPI and str(constraint.bound.unit) == NCI_UNIT:
            # A KPI intent names no cell, so the cell condition is membership,
            # not identity: the UE is served by one of the deployment's cells
            # throughout the hold.  The bound is kept as it stands -- MEMBER_OF
            # does not read its value, and the Kernel still checks that its unit
            # matches the samples'.
            constraint = replace(constraint, operator=ComparisonOperator.MEMBER_OF,
                                 allowed_values=spec.membership_values)
            description = ("the UE stayed served by one of the deployment's cells "
                           "for the whole hold")
        renamed_predicate = replace(
            predicate,
            predicate_id=f"{tag}/{predicate.predicate_id}",
            mandatory=True,
            constraint=constraint,
            description=description,
        )
        folded.predicates.append(renamed_predicate)
        predicate_ids.append(renamed_predicate.predicate_id)
    intent_predicates[tag] = tuple(predicate_ids)
    for watchdog in bundle.harm.watchdogs:
        folded.watchdogs.append(replace(
            watchdog,
            contract_id=f"{watchdog.contract_id}@{tag}",
            watchdog_id=f"{watchdog.watchdog_id}@{tag}",
            trigger=replace(watchdog.trigger,
                            measurement_ref=_rename_ref(watchdog.trigger.measurement_ref,
                                                        measurement_names)),
        ))
    for bound in bundle.harm.bounds:
        folded.bounds.append(replace(
            bound,
            bound_id=f"{bound.bound_id}@{tag}",
            uncertainty_ref=_rename_ref(bound.uncertainty_ref, measurement_names),
        ))
    folded.reserve_ms += float(bundle.harm.reserve.value)
    folded.missing_charge_ms = max(folded.missing_charge_ms,
                                   float(bundle.harm.missing_interval_charge.value))
    folded.hold_ms = max(folded.hold_ms, int(bundle.target.hold_ms))
    if spec.ue_id not in steering_measurements:
        minimum = next((m for m in renamed_measurements if m.contract_id.endswith(f"-min@{tag}")), None)
        maximum = next((m for m in renamed_measurements if m.contract_id.endswith(f"-max@{tag}")), None)
        if minimum is None or maximum is None:
            raise JointCompositionError(
                f"{spec.family} states no serving-cell min/max measurement pair for "
                f"UE {spec.ue_id}; the steering actuator has nothing to read back")
        steering_measurements[spec.ue_id] = (minimum, maximum)


# --------------------------------------------------------------------------- #
# the joint bundle
# --------------------------------------------------------------------------- #


def compose_joint(
    *,
    intents: Sequence[IntentSpec],
    steering_axes: Sequence[SteeringAxisSpec],
    cap_axes: Sequence[CapAxisSpec] = (),
    priority_axes: Sequence[PriorityAxisSpec] = (),
    mcs_axes: Sequence[McsBoundsAxisSpec] = (),
    attenuation_axes: Sequence[TxAttenuationAxisSpec] = (),
    slice_quota_axes: Sequence[SlicePrbQuotaAxisSpec] = (),
    deployment_binding: DeploymentBinding,
    budget_trials: int,
    deadline_ms: Optional[int] = None,
    hold_ms: Optional[int] = None,
    cell_id: str = "NRCellDU-1",
    max_catalog_cardinality: int = DEFAULT_MAX_CATALOG_CARDINALITY,
) -> JointComposition:
    """Compose one joint case from any number of intents and action axes.

    ``budget_trials`` becomes the case policy's trial cap: it is the search
    budget, frozen into the epoch so the Kernel -- not the searcher -- ends
    the case when it is spent.

    ``hold_ms`` is how long the case holds each trial's observation window
    open.  A sitting that states per-KPI observation rules (contract v2
    section 6) passes ``max(settle + window)`` over the kinds its ``T`` uses;
    omitted, the fold of the objective families' own holds stands, exactly as
    before those rules existed.

    ``mcs_axes``, ``attenuation_axes`` and ``slice_quota_axes`` are the cell-
    and slice-scoped knobs.  They are declared exactly like the UE-scoped ones
    -- a configuration readback that proves the write landed, an actuator
    bound to the adapter that carries the policy type, a baseline the safe
    state restores -- but they are scoped to a cell or an S-NSSAI rather than
    a UE, so their readback is keyed by ``cellId`` / ``sNssai`` and never
    charged to a UE that happens to be on that cell.  Like the priority axis,
    they add no separate certified harm bound: this deployment publishes no
    calibrated aggregate for the non-intent population of a cell, and a bound
    invented here would be a number nobody measured.

    ``max_catalog_cardinality`` bounds what one epoch may freeze.  The catalog
    is the *product* of the exposed axes, so a sitting that turns every axis on
    for two UEs and two cells spans 248832 candidates; over the ceiling this
    function raises :class:`CatalogTooLargeError` naming each axis kind and its
    size, before a single contract is built.
    """
    intents = tuple(intents)
    if not intents:
        raise JointCompositionError("a joint case needs at least one intent")
    ids = [item.intent_id for item in intents]
    if len(set(ids)) != len(ids):
        raise JointCompositionError(f"intent ids must be unique: {ids}")
    if int(budget_trials) <= 0:
        raise JointCompositionError("the search budget must be a positive number of trials")
    steering_axes = tuple(steering_axes)
    cap_axes = tuple(cap_axes)
    priority_axes = tuple(priority_axes)
    mcs_axes = tuple(mcs_axes)
    attenuation_axes = tuple(attenuation_axes)
    slice_quota_axes = tuple(slice_quota_axes)
    axis_specs = (*steering_axes, *cap_axes, *priority_axes, *mcs_axes,
                  *attenuation_axes, *slice_quota_axes)
    axes = [item.axis for item in axis_specs]
    if len(set(axes)) != len(axes):
        raise JointCompositionError(f"action axes must be unique: {axes}")
    # Before anything is built: the catalog the search would have to walk.
    _refuse_oversized_catalog(axis_specs, max_catalog_cardinality)
    steered = {item.ue_id for item in steering_axes}
    for spec in intents:
        if spec.ue_id not in steered:
            raise JointCompositionError(
                f"intent {spec.intent_id} is about UE {spec.ue_id}, which no steering "
                "axis covers; every intent UE needs its steering action declared")

    folded = _Folded()
    intent_predicates: Dict[str, Tuple[str, ...]] = {}
    steering_measurements: Dict[str, Tuple[MeasurementContract, MeasurementContract]] = {}
    for spec in intents:
        _fold_intent(spec, deployment_binding, folded, intent_predicates, steering_measurements)
    # The case is held open for as long as the *sitting's* observation rules
    # need (contract v2 section 6: max(settle + window) over the KPI kinds T
    # uses).  With no rules stated the fold of the families' own holds stands,
    # which is what every caller before those rules existed got.
    stated_hold = None if hold_ms is None else max(1, int(hold_ms))
    hold_ms = stated_hold if stated_hold is not None else max(folded.hold_ms, 1)
    if stated_hold is not None:
        # The case has to actually hold its observation open for as long as the
        # sitting says: the poll schedule is derived from the *measurement*
        # contracts (``gui.operator.sources.kernel_live.polling_plan``), so a
        # stated hold that only reached the target contract would freeze a
        # window nobody ever polls.
        folded.measurements = [replace(item, hold_ms=stated_hold)
                               for item in folded.measurements]
    cadence_ms = max(int(m.cadence_ms) for m in folded.measurements)

    # -- supplementary axes: their configuration readback and their debt ---- #
    for cap in cap_axes:
        action = SUPPLEMENTARY_ACTIONS[CAP_ACTION_ID]
        family_tag = f"joint@{cap.ue_id}"
        counter, measurement = _configuration_contracts(
            family=family_tag, action=action, scope_id=cap.ue_id,
            deployment=deployment_binding, cadence_ms=cadence_ms, hold_ms=hold_ms)
        controlled_counter, controlled_measurement = _throughput_contracts(
            family=family_tag, scope_key="controlledUeId", scope_id=cap.ue_id,
            suffix="controlled", deployment=deployment_binding, hold_ms=hold_ms,
            cadence_ms=cadence_ms)
        folded.counters.extend((counter, controlled_counter))
        folded.measurements.extend((measurement, controlled_measurement))
        folded.watchdogs.append(WatchdogContract(
            **_identity(f"watchdog/{family_tag}/controlled-ue-cap"),
            watchdog_id=f"wd/{family_tag}/controlled-ue-cap",
            trigger=TypedConstraint(
                controlled_measurement.contract_id, ComparisonOperator.GREATER_OR_EQUAL,
                _quantity(cap.harm_reserve_kbps, "kbit/s", cap.calibration_ref)),
            action=WatchdogAction.STOP_AND_ROLLBACK, max_evaluation_latency_ms=1000))
        folded.bounds.append(CertifiedHarmBound(
            f"bound/{family_tag}/controlled-ue-cap",
            _quantity(cap.harm_reserve_kbps, "kbit/s", f"{cap.calibration_ref}#calibration"),
            _quantity(cap.harm_reserve_kbps * 0.1, "kbit/s", f"{cap.calibration_ref}#margin"),
            _quantity(cap.harm_reserve_kbps * 1.1, "kbit/s", f"{cap.calibration_ref}#admission"),
            f"{controlled_measurement.contract_id}#uncertainty",
            {"controlledUeId": cap.ue_id}, enforced_timeout_ms_default(),
            (cap.calibration_ref,),
            f"proof/{family_tag}/controlled-ue-cap"))
    for weight in priority_axes:
        action = SUPPLEMENTARY_ACTIONS[PRIORITY_ACTION_ID]
        counter, measurement = _configuration_contracts(
            family=f"joint@{weight.ue_id}", action=action, scope_id=weight.ue_id,
            deployment=deployment_binding, cadence_ms=cadence_ms, hold_ms=hold_ms)
        folded.counters.append(counter)
        folded.measurements.append(measurement)
    # The cell- and slice-scoped axes: a configuration readback each, keyed by
    # the scope the action is actually written at.  No throughput contract and
    # no certified bound -- see this function's docstring.
    for spec in (*mcs_axes, *attenuation_axes, *slice_quota_axes):
        counter, measurement = _configuration_contracts(
            family=_family_tag(spec), action=spec.action,
            scope_id=_readback_scope_id(spec), deployment=deployment_binding,
            cadence_ms=cadence_ms, hold_ms=hold_ms)
        folded.counters.append(counter)
        folded.measurements.append(measurement)

    # -- the actuators, one per (action, scope) ----------------------------- #
    capability_id = "capability/joint"
    actuators: List[ActuatorBinding] = []
    actuators_by_axis: Dict[str, ActuatorBinding] = {}
    action_axes: Dict[str, str] = {}
    axis_baselines: Dict[str, str] = {}
    parameter_space: Dict[str, Tuple[str, ...]] = {}
    for steer in steering_axes:
        minimum, _maximum = steering_measurements.get(steer.ue_id, (None, None))
        if minimum is None:
            raise JointCompositionError(
                f"steering axis for UE {steer.ue_id} has no intent measuring that UE; "
                "an actuator without a readback measurement cannot be admitted")
        actuator = ActuatorBinding(
            **_identity(f"actuator/joint/steer@{steer.ue_id}"),
            capability_ref=capability_id, path=ActuatorPath.OFFICIAL_ORAN_DYNAMIC,
            policy_type_id=STEERING_POLICY_TYPE_ID,
            service_model={"serviceModel": "E2SM-RC", "style": "3", "action": "1",
                           "profile": "E2SM-RC-STYLE3-ACTION1"},
            readback_measurement_ref=minimum.contract_id,
            deployment_binding_ref=deployment_binding.contract_id)
        actuators.append(actuator)
        actuators_by_axis[steer.axis] = actuator
        action_axes[steer.action_id] = steer.axis
        axis_baselines[steer.axis] = steer.baseline
        parameter_space[steer.axis] = steer.values
    for cap in cap_axes:
        readback = next(m for m in folded.measurements
                        if m.contract_id == f"measurement/joint@{cap.ue_id}/{CAP_ACTION_ID}")
        actuator = ActuatorBinding(
            **_identity(f"actuator/joint/cap@{cap.ue_id}"),
            capability_ref=capability_id, path=ActuatorPath.OFFICIAL_ORAN_DYNAMIC,
            policy_type_id=SUPPLEMENTARY_ACTIONS[CAP_ACTION_ID].policy_type_id,
            service_model={"serviceModel": "E2SM-RC", "style": "2", "action": "102",
                           "profile": "E2SM-RC-STYLE2-ACTION102"},
            readback_measurement_ref=readback.contract_id,
            deployment_binding_ref=deployment_binding.contract_id)
        actuators.append(actuator)
        actuators_by_axis[cap.axis] = actuator
        action_axes[cap.action_id] = cap.axis
        axis_baselines[cap.axis] = cap.baseline
        parameter_space[cap.axis] = cap.values
    for weight in priority_axes:
        readback = next(m for m in folded.measurements
                        if m.contract_id == f"measurement/joint@{weight.ue_id}/{PRIORITY_ACTION_ID}")
        actuator = ActuatorBinding(
            **_identity(f"actuator/joint/priority@{weight.ue_id}"),
            capability_ref=capability_id, path=ActuatorPath.OFFICIAL_ORAN_DYNAMIC,
            policy_type_id=SUPPLEMENTARY_ACTIONS[PRIORITY_ACTION_ID].policy_type_id,
            service_model={"serviceModel": "E2SM-RC", "style": "2", "action": "103",
                           "profile": "E2SM-RC-STYLE2-ACTION103"},
            readback_measurement_ref=readback.contract_id,
            deployment_binding_ref=deployment_binding.contract_id)
        actuators.append(actuator)
        actuators_by_axis[weight.axis] = actuator
        action_axes[weight.action_id] = weight.axis
        axis_baselines[weight.axis] = weight.baseline
        parameter_space[weight.axis] = weight.values
    for spec in (*mcs_axes, *attenuation_axes, *slice_quota_axes):
        declared = spec.action
        readback = next(
            m for m in folded.measurements
            if m.contract_id == f"measurement/{_family_tag(spec)}/{declared.action_id}")
        actuator = ActuatorBinding(
            **_identity(f"actuator/joint/{spec.action_id}"),
            capability_ref=capability_id, path=ActuatorPath.OFFICIAL_ORAN_DYNAMIC,
            policy_type_id=declared.policy_type_id,
            service_model=dict(_NON_UE_SERVICE_MODELS[declared.action_id]),
            readback_measurement_ref=readback.contract_id,
            deployment_binding_ref=deployment_binding.contract_id)
        actuators.append(actuator)
        actuators_by_axis[spec.axis] = actuator
        action_axes[spec.action_id] = spec.axis
        axis_baselines[spec.axis] = spec.baseline
        parameter_space[spec.axis] = spec.values

    capability = CapabilityManifest(
        **_identity(capability_id), capability_id=capability_id,
        supported_objectives=tuple(dict.fromkeys(item.family for item in intents)) + (JOINT_FAMILY,),
        constraints=tuple(p.constraint for p in folded.predicates),
        actuator_refs=tuple(a.contract_id for a in actuators),
        measurement_refs=tuple(m.contract_id for m in folded.measurements),
        interface_versions={"a1p": "2", "e2smRc": "1.03", "e2smKpm": "2.03"})
    option = TargetOption(**_identity("option/joint"), capability_ref=capability_id,
                          parameter_space=parameter_space)
    ue_ids = tuple(dict.fromkeys(item.ue_id for item in intents))
    scope_selector = {"ueIds": ",".join(ue_ids), "cellId": str(cell_id)}
    target = TargetContract(
        **_identity("target/joint"), objective_family=JOINT_FAMILY,
        scope_selector=scope_selector, predicates=tuple(folded.predicates),
        options=(option,), hold_ms=hold_ms)
    vector = TargetVector(**_identity("vector/joint"), ordered_target_refs=(target.contract_id,))
    release = TargetReleasePolicy(**_identity("release/joint"))
    harm = HarmContract(
        **_identity("harm/joint"), harm_kind=HarmKind.TRIAL_INDUCED,
        scope_selector=scope_selector,
        reserve=_quantity(folded.reserve_ms, "ms", "joint/reserve-sum"),
        bounds=tuple(folded.bounds), watchdogs=tuple(folded.watchdogs),
        missing_interval_charge=_quantity(max(folded.missing_charge_ms, 1.0), "ms", "joint/missing"))
    trials = int(budget_trials)
    horizon = int(deadline_ms) if deadline_ms is not None else (
        trials * (hold_ms + 180_000) + 600_000)
    case_policy = CoordinationCasePolicy(
        **_identity("case-policy/joint"), deadline_ms=horizon, max_trials=trials,
        max_proposals=2 * trials + 2, target_release_policy_ref=release.contract_id,
        harm_contract_refs=(harm.contract_id,))
    composition = CompositionManifest(
        **_identity("composition/joint"), composition_id="composition/joint",
        capability_refs=(capability_id,))
    scope: Dict[str, Any] = {
        "ueIds": list(ue_ids), "cellId": str(cell_id),
        "intents": {item.intent_id: {"family": item.family, "ueId": item.ue_id,
                                     "targetCell": str(int(item.target_nci))}
                    for item in intents},
    }
    for steer in steering_axes:
        scope[f"ue@{steer.ue_id}"] = {"ueId": steer.ue_id}
    for cap in cap_axes:
        scope[f"controlledUe@{cap.ue_id}"] = {"cellId": str(int(cap.cell_nci)), "ueId": cap.ue_id}
    for weight in priority_axes:
        scope.setdefault(f"controlledUe@{weight.ue_id}",
                         {"cellId": str(int(weight.cell_nci)), "ueId": weight.ue_id})
    for spec in (*mcs_axes, *attenuation_axes):
        scope.setdefault(spec.scope, {"cellId": str(int(spec.cell_nci))})
    for spec in slice_quota_axes:
        entry: Dict[str, Any] = {"sst": str(spec.sst), "sNssai": spec.scope_id}
        if spec.sd:
            entry["sd"] = spec.sd
        scope.setdefault(spec.scope, entry)
    bundle = ObjectiveContractBundle(
        family=JOINT_FAMILY, counters=tuple(folded.counters),
        measurements=tuple(folded.measurements), target=target, vector=vector,
        release=release, case_policy=case_policy, watchdogs=tuple(folded.watchdogs),
        harm=harm, deployment=deployment_binding, actuators=tuple(actuators),
        capabilities=(capability,), composition=composition,
        baseline_config=dict(axis_baselines), safe_state=dict(axis_baselines),
        scope=scope, sample_scope={"ueIds": ",".join(ue_ids)},
        component_predicates=dict(intent_predicates))
    return JointComposition(
        bundle=bundle, intents=intents, steering_axes=steering_axes, cap_axes=cap_axes,
        priority_axes=priority_axes, intent_predicates=dict(intent_predicates),
        action_axes=action_axes, axis_baselines=axis_baselines,
        steering_measurements=steering_measurements, actuators_by_axis=actuators_by_axis,
        mcs_axes=mcs_axes, attenuation_axes=attenuation_axes,
        slice_quota_axes=slice_quota_axes,
        axis_scopes={item.axis: item.scope for item in axis_specs})
