"""The central joint-effect predictor: what a configuration is expected to do.

A single-call model cannot reach for a tool half-way through generating its
answer, so the executor asks this module **first** and hands the answer over as
``input.effect_evidence`` (contract v2 section 4).  Control ranks its
candidates with it, Trajectory reads the same numbers back as
``control_candidates[].predicted``, and the deterministic fallbacks order their
moves by it.

The model is an analytic capacity-share of the deployed carrier: each cell has
a capacity in Mbps, each UE on it a link efficiency and a PF weight, a PRB cap
is a fraction of that cell's capacity, and the offered load is the ceiling
nobody exceeds.  One weighted redistribution pass gives the leftover of the
UEs that are load-limited to the ones that are not, and a cell change costs a
handover transient over the observation window.

Three of the six axes are not a UE's.  A cell's **MCS ceiling** and its
**transmit attenuation** are properties of the cell, so they clip every UE on
it by the same factor -- the ceiling by the ratio of 3GPP spectral
efficiencies, the attenuation by what it takes off a nominal operating SNR --
and a **slice quota** clips the UEs of that slice *together*, as a share of
the cell they contend on rather than as a per-UE limit.  Each is coarse and
each is stated in :meth:`JointEffectPredictor.describe`, because a model an
agent ranks candidates with has to say what it does and does not account for:
the MCS *floor* costs reliability rather than rate, and nothing here estimates
the retransmissions that would follow.

It is **separate code from the emulator on purpose**.  It never reads the
emulator's state, its seed or its noise: a predictor that can see the ground
truth is not a predictor, and the paper's claim is about an agent that acts on
estimates.  Live, the same arithmetic is calibrated by
:meth:`JointEffectPredictor.calibrate` from what was actually observed.

Every number comes back with an uncertainty, and anything the model has no
basis for comes back as ``unknown`` rather than as a confident guess.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from assurance.coordination.tc import (
    KPI_DEADLINE_RATIO, KPI_GOODPUT, KPI_SERVING_CELL, UNKNOWN, kpi_key,
)

__all__ = [
    "AXIS_CAP",
    "AXIS_MCS_BOUNDS",
    "AXIS_PF_WEIGHT",
    "AXIS_SERVING_CELL",
    "AXIS_SLICE_QUOTA",
    "AXIS_TX_ATTENUATION",
    "BASELINE_MCS_BOUNDS",
    "BASELINE_SLICE_QUOTA",
    "BASELINE_TX_ATTENUATION_DB",
    "DEFAULT_ECHO_PAYLOAD_BYTES",
    "DEFAULT_ECHO_PROCESSING_MS",
    "DEFAULT_NOMINAL_SNR_DB",
    "DEFAULT_UNCERTAINTY",
    "JointEffectPredictor",
    "MAXIMUM_MCS_INDEX",
    "NetworkState",
    "PredictedKpi",
]

#: The six action axes this deployment exposes, as named in
#: ``assurance/objectives/joint.py``.  The first three are scoped to a UE, the
#: next two to a cell, the last to an S-NSSAI -- which is why the model reads
#: them by kind before it reads the target, rather than assuming every axis
#: names a UE.
AXIS_SERVING_CELL = "servingCell"
AXIS_CAP = "dlPrbCap"
AXIS_PF_WEIGHT = "pfWeight"
AXIS_MCS_BOUNDS = "dlMcsBounds"
AXIS_TX_ATTENUATION = "txAttenuationDb"
AXIS_SLICE_QUOTA = "slicePrbQuota"

#: The value each new axis rests at when nothing is applied.
BASELINE_MCS_BOUNDS = "0..28"
BASELINE_TX_ATTENUATION_DB = 0.0
BASELINE_SLICE_QUOTA = "0:1:100"

#: The top of the DL MCS index range this deployment's link adaptation uses.
MAXIMUM_MCS_INDEX = 28

#: 3GPP TS 38.214 table 5.1.3.1-1 (64QAM), spectral efficiency per MCS index.
#: The MCS ceiling is read off this table and divided by the unconstrained
#: ceiling's, so lowering ``max`` lowers the achievable rate by the ratio the
#: modulation and coding actually lose -- not by a straight-line guess.  The
#: floor is deliberately *not* modelled: raising ``min`` costs reliability, not
#: rate, and this model has no basis for the retransmissions that would follow.
_MCS_SPECTRAL_EFFICIENCY: Tuple[float, ...] = (
    0.2344, 0.3066, 0.3770, 0.4902, 0.6016, 0.7402, 0.8770, 1.0273, 1.1758,
    1.3252, 1.3281, 1.4766, 1.6953, 1.9141, 2.1602, 2.4063, 2.5703, 2.5664,
    2.7305, 3.0293, 3.3223, 3.6094, 3.9023, 4.2129, 4.5234, 4.8164, 5.1152,
    5.3320, 5.5547,
)

#: The operating SNR a UE at the cell's nominal attenuation is assumed to see.
#: Attenuation is charged straight against it and the rate follows Shannon, so
#: 0 dB of attenuation is the full rate and every dB after that costs what the
#: log costs.  Coarse on purpose: the point is the *direction and rough size*
#: of the effect, and the band around it is stated with every prediction.
DEFAULT_NOMINAL_SNR_DB = 20.0

#: The relative uncertainty an uncalibrated prediction carries (contract v2
#: section 4: plus or minus 15 per cent).
DEFAULT_UNCERTAINTY = 0.15

#: The carrier the lab actually runs (``docs/handoff``): 24 PRB, so an
#: uncapped grant is 24 PRB and a cap is a fraction of that.
DEFAULT_PRB_TOTAL = 24.0

#: A cell change costs this long at zero goodput before the UE is serving again.
DEFAULT_HANDOVER_TRANSIENT_MS = 2000.0

#: What a tagged echo request costs on the wire and at the far end.  A command
#: echo is a small datagram, so its serialization is a fraction of a
#: millisecond at any share this carrier reaches -- which is the point: the
#: deadline-success ratio moves with the **queueing** the share leaves, not
#: with the payload.  Both are coarse and both are stated in
#: :meth:`JointEffectPredictor.describe`.
DEFAULT_ECHO_PAYLOAD_BYTES = 200.0
DEFAULT_ECHO_PROCESSING_MS = 5.0

#: The deadline a UE's tagged echo flow is measured against when the sitting
#: names none.  Nothing predicts a ratio without one: a requirement with no
#: deadline is a gap the intake checklist closes, not a number this module
#: guesses at.
DEFAULT_ECHO_DEADLINE_MS = 50.0


def _as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(number) else number


def _number_text(value: Any) -> str:
    """``24.0`` is written ``"24"``: an axis value is a name, not a quantity."""
    number = _as_float(value)
    if number is None:
        return str(value)
    return str(int(number)) if float(number).is_integer() else str(number)


def _axis_parts(axis: str) -> Tuple[str, str]:
    text = str(axis or "")
    kind, _, target = text.partition("@")
    return kind, target


def _mcs_ceiling_factor(bounds: Any) -> float:
    """How much of the unconstrained rate an MCS ceiling leaves.

    ``"0..28"`` leaves all of it; ``"0..16"`` leaves the ratio of the two
    spectral efficiencies.  The floor is read and ignored on purpose -- see
    :data:`_MCS_SPECTRAL_EFFICIENCY`.  An unparseable value is treated as the
    baseline rather than as a reason to guess.
    """
    _low, separator, high = str(bounds or "").partition("..")
    if not separator:
        return 1.0
    try:
        ceiling = int(high)
    except ValueError:
        return 1.0
    ceiling = max(0, min(MAXIMUM_MCS_INDEX, ceiling))
    return (_MCS_SPECTRAL_EFFICIENCY[ceiling]
            / _MCS_SPECTRAL_EFFICIENCY[MAXIMUM_MCS_INDEX])


def _attenuation_factor(attenuation_db: Any,
                        nominal_snr_db: float = DEFAULT_NOMINAL_SNR_DB) -> float:
    """How much of the rate is left after *attenuation_db* of lost power.

    Attenuation comes straight off the operating SNR and the rate follows
    ``log2(1 + SNR)``, so the effect is monotone, one-directional and largest
    where the link was already marginal.  A cell attenuated past its own
    nominal SNR keeps a small positive rate rather than falling to zero: the
    model has no basis for a cliff, and predicting one would rank a candidate
    the search has not tried as impossible.
    """
    attenuation = _as_float(attenuation_db, 0.0) or 0.0
    if attenuation <= 0:
        return 1.0
    nominal = max(1.0, float(nominal_snr_db))
    full = math.log2(1.0 + 10.0 ** (nominal / 10.0))
    left = math.log2(1.0 + 10.0 ** ((nominal - attenuation) / 10.0))
    return max(0.0, min(1.0, left / full))


def _quota_share(quota: Any) -> float:
    """The fraction of a cell the UEs of a slice may hold together.

    ``maxPrbPolicyRatio`` is the operative field: ``min`` is a guarantee this
    coarse model has no contention to test it against, and ``dedicated`` is a
    reservation nobody in the lab configures.  An unparseable value leaves the
    slice unconstrained.
    """
    parts = str(quota or "").split(":")
    if len(parts) != 3:
        return 1.0
    try:
        maximum = int(parts[2])
    except ValueError:
        return 1.0
    return max(0.0, min(100, maximum)) / 100.0


@dataclass
class _Applied:
    """The configuration a prediction is made against, read out by scope."""

    serving: Dict[str, str] = field(default_factory=dict)
    caps: Dict[str, float] = field(default_factory=dict)
    weights: Dict[str, float] = field(default_factory=dict)
    mcs_bounds: Dict[str, str] = field(default_factory=dict)
    attenuation: Dict[str, float] = field(default_factory=dict)
    quota: Dict[str, str] = field(default_factory=dict)

    def cell_factor(self, cell: str) -> float:
        """What a cell's own knobs leave of a UE's achievable rate on it."""
        return (_mcs_ceiling_factor(self.mcs_bounds.get(cell, BASELINE_MCS_BOUNDS))
                * _attenuation_factor(self.attenuation.get(
                    cell, BASELINE_TX_ATTENUATION_DB)))


@dataclass(frozen=True)
class PredictedKpi:
    """One predicted KPI: a value, and how far it may be off."""

    value: Any
    uncertainty: Any = DEFAULT_UNCERTAINTY

    def as_pair(self) -> Tuple[Any, Any]:
        return (self.value, self.uncertainty)


@dataclass
class NetworkState:
    """What the predictor needs to know about the deployment right now.

    Everything is optional and everything has a stated default, because the
    predictor is asked before the first trial as well as after the tenth.
    """

    cells: Dict[str, float] = field(default_factory=dict)
    ues: Dict[str, str] = field(default_factory=dict)
    offered_load_mbps: Dict[str, float] = field(default_factory=dict)
    link_efficiency: Dict[str, Dict[str, float]] = field(default_factory=dict)
    pf_weights: Dict[str, float] = field(default_factory=dict)
    caps_prb: Dict[str, float] = field(default_factory=dict)
    #: cell -> its applied ``"<min>..<max>"`` DL MCS bounds.
    mcs_bounds: Dict[str, str] = field(default_factory=dict)
    #: cell -> its applied transmit attenuation, in dB below full gain.
    tx_attenuation_db: Dict[str, float] = field(default_factory=dict)
    #: UE -> the SST of the slice it is carried on.  A UE with no slice is in
    #: no quota group and a quota never touches it.
    ue_slices: Dict[str, str] = field(default_factory=dict)
    #: SST -> its applied ``"<dedicated>:<min>:<max>"`` PRB policy ratios.
    slice_quota: Dict[str, str] = field(default_factory=dict)
    #: UE -> the deadline ``D`` its tagged echo flow is measured against, in
    #: milliseconds.  A UE that is not in this mapping has no such flow and no
    #: :data:`KPI_DEADLINE_RATIO` is predicted for it -- an intent that names a
    #: deadline puts it here, nothing else does.
    echo_deadline_ms: Dict[str, float] = field(default_factory=dict)
    prb_total: float = DEFAULT_PRB_TOTAL
    window_ms: float = 0.0
    handover_transient_ms: float = DEFAULT_HANDOVER_TRANSIENT_MS
    unselected_function_rule: str = "baseline"

    def __post_init__(self) -> None:
        self.cells = {str(k): float(v) for k, v in dict(self.cells or {}).items()}
        self.ues = {str(k): str(v) for k, v in dict(self.ues or {}).items()}
        self.offered_load_mbps = {str(k): float(v) for k, v
                                  in dict(self.offered_load_mbps or {}).items()}
        self.link_efficiency = {
            str(ue): {str(cell): float(value) for cell, value in dict(row or {}).items()}
            for ue, row in dict(self.link_efficiency or {}).items()}
        self.pf_weights = {str(k): float(v) for k, v in dict(self.pf_weights or {}).items()}
        self.caps_prb = {str(k): float(v) for k, v in dict(self.caps_prb or {}).items()}
        self.mcs_bounds = {str(k): str(v) for k, v in dict(self.mcs_bounds or {}).items()}
        self.tx_attenuation_db = {str(k): float(v) for k, v
                                  in dict(self.tx_attenuation_db or {}).items()}
        self.ue_slices = {str(k): str(v) for k, v in dict(self.ue_slices or {}).items()}
        self.slice_quota = {str(k): str(v) for k, v
                            in dict(self.slice_quota or {}).items()}
        self.echo_deadline_ms = {str(k): float(v) for k, v
                                 in dict(self.echo_deadline_ms or {}).items()
                                 if _as_float(v) is not None and float(v) > 0}
        self.prb_total = float(self.prb_total or DEFAULT_PRB_TOTAL)

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "NetworkState":
        """Reads the ``input.network_state`` shape the executor builds."""
        record = dict(record or {})
        cells = dict(record.get("cells") or {})
        capacities = {str(nci): _as_float(
            entry.get("capacityMbps") if isinstance(entry, Mapping) else entry, 0.0)
            for nci, entry in cells.items()}
        ues: Dict[str, str] = {}
        loads: Dict[str, float] = {}
        efficiency: Dict[str, Dict[str, float]] = {}
        weights: Dict[str, float] = {}
        caps: Dict[str, float] = {}
        slices: Dict[str, str] = {}
        deadlines: Dict[str, float] = {}
        for ue_id, entry in dict(record.get("ues") or {}).items():
            if not isinstance(entry, Mapping):
                ues[str(ue_id)] = str(entry)
                continue
            ues[str(ue_id)] = str(entry.get("servingCell", ""))
            load = _as_float(entry.get("offeredLoadMbps"))
            if load is not None:
                loads[str(ue_id)] = load
            weight = _as_float(entry.get("pfWeight"))
            if weight is not None:
                weights[str(ue_id)] = weight
            cap = _as_float(entry.get("dlPrbCap"))
            if cap is not None:
                caps[str(ue_id)] = cap
            row = entry.get("linkEfficiency")
            if isinstance(row, Mapping):
                efficiency[str(ue_id)] = {str(k): float(v) for k, v in row.items()}
            sst = entry.get("sst")
            if sst not in (None, ""):
                slices[str(ue_id)] = str(sst)
            deadline = _as_float(entry.get("echoDeadlineMs"))
            if deadline is not None and deadline > 0:
                deadlines[str(ue_id)] = deadline
        bounds: Dict[str, str] = {}
        attenuation: Dict[str, float] = {}
        for nci, entry in cells.items():
            if not isinstance(entry, Mapping):
                continue
            stated = entry.get("dlMcsBounds")
            if stated:
                bounds[str(nci)] = str(stated)
            attenuated = _as_float(entry.get("txAttenuationDb"))
            if attenuated is not None:
                attenuation[str(nci)] = attenuated
        quota = {str(sst): str(value) for sst, value
                 in dict(record.get("slices") or {}).items() if value}
        return cls(cells=capacities, ues=ues, offered_load_mbps=loads,
                   link_efficiency=efficiency, pf_weights=weights, caps_prb=caps,
                   mcs_bounds=bounds, tx_attenuation_db=attenuation,
                   ue_slices=slices, slice_quota=quota,
                   echo_deadline_ms=deadlines,
                   prb_total=_as_float(record.get("prbTotal"), DEFAULT_PRB_TOTAL),
                   window_ms=_as_float(record.get("windowMs"), 0.0) or 0.0,
                   unselected_function_rule=str(
                       record.get("unselectedFunctionRule", "baseline") or "baseline"))

    def applied_configuration(self) -> Dict[str, str]:
        """The axis vector the state is in right now.

        Written the way the catalog writes its values -- ``"24"``, not
        ``"24.0"`` -- so a baseline read from the state and a baseline read
        from the catalog are the same string.
        """
        configuration: Dict[str, str] = {}
        for ue_id, cell in self.ues.items():
            configuration[f"{AXIS_SERVING_CELL}@{ue_id}"] = str(cell)
            configuration[f"{AXIS_CAP}@{ue_id}"] = _number_text(
                self.caps_prb.get(ue_id, self.prb_total))
            configuration[f"{AXIS_PF_WEIGHT}@{ue_id}"] = _number_text(
                self.pf_weights.get(ue_id, 8.0))
        for cell in self.cells:
            configuration[f"{AXIS_MCS_BOUNDS}@{cell}"] = str(
                self.mcs_bounds.get(cell, BASELINE_MCS_BOUNDS))
            configuration[f"{AXIS_TX_ATTENUATION}@{cell}"] = (
                f"{self.tx_attenuation_db.get(cell, BASELINE_TX_ATTENUATION_DB):.1f}")
        for sst in sorted(set(self.ue_slices.values()) | set(self.slice_quota)):
            configuration[f"{AXIS_SLICE_QUOTA}@{sst}"] = str(
                self.slice_quota.get(sst, BASELINE_SLICE_QUOTA))
        return configuration

    def to_record(self) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "cells": {nci: {
                "capacityMbps": value,
                "dlMcsBounds": self.mcs_bounds.get(nci, BASELINE_MCS_BOUNDS),
                "txAttenuationDb": self.tx_attenuation_db.get(
                    nci, BASELINE_TX_ATTENUATION_DB)}
                for nci, value in self.cells.items()},
            "ues": {ue: {"servingCell": self.ues.get(ue, ""),
                         "offeredLoadMbps": self.offered_load_mbps.get(ue),
                         "pfWeight": self.pf_weights.get(ue, 8.0),
                         "dlPrbCap": self.caps_prb.get(ue, self.prb_total),
                         "sst": self.ue_slices.get(ue),
                         **({"echoDeadlineMs": self.echo_deadline_ms[ue]}
                            if ue in self.echo_deadline_ms else {}),
                         "linkEfficiency": dict(self.link_efficiency.get(ue, {}))}
                    for ue in self.ues},
            "prbTotal": self.prb_total,
            "unselectedFunctionRule": self.unselected_function_rule}
        if self.ue_slices or self.slice_quota:
            record["slices"] = {
                sst: self.slice_quota.get(sst, BASELINE_SLICE_QUOTA)
                for sst in sorted(set(self.ue_slices.values()) | set(self.slice_quota))}
        return record


class JointEffectPredictor:
    """The one effect model every method is given, hardware-free and live.

    ``predict`` answers ``{kpiKey: (value, uncertainty)}`` for a whole
    configuration -- joint, not one axis at a time, which is the entire point:
    capping one UE is what frees the cell for the other.
    """

    def __init__(self, state: Optional[NetworkState] = None, *,
                 uncertainty: float = DEFAULT_UNCERTAINTY,
                 handover_transient_ms: float = DEFAULT_HANDOVER_TRANSIENT_MS) -> None:
        self.state = state or NetworkState()
        self.uncertainty = float(uncertainty)
        self.handover_transient_ms = float(handover_transient_ms)
        #: What ``calibrate`` has learned, per cell and per UE (1.0 = untouched).
        self.capacity_scale: Dict[str, float] = {}
        self.efficiency_scale: Dict[str, float] = {}
        self.calibration_samples = 0

    # -- the model ---------------------------------------------------------- #

    def _capacity(self, cell: str) -> float:
        return (self.state.cells.get(cell, 0.0)
                * float(self.capacity_scale.get(cell, 1.0)))

    def _efficiency(self, ue: str, cell: str) -> float:
        base = self.state.link_efficiency.get(ue, {}).get(cell, 1.0)
        return float(base) * float(self.efficiency_scale.get(ue, 1.0))

    def _read(self, configuration: Mapping[str, Any], state: NetworkState
              ) -> "_Applied":
        """The applied vector this configuration describes, by scope.

        Three of the six axes name a cell or an S-NSSAI rather than a UE, so
        the kind is read first: an axis whose target is not a UE is not a UE's
        axis with an unknown UE, and dropping it would silently predict the
        baseline for a knob the candidate actually moves.
        """
        applied = _Applied(
            serving=dict(state.ues),
            caps={ue: state.caps_prb.get(ue, state.prb_total) for ue in state.ues},
            weights={ue: state.pf_weights.get(ue, 8.0) for ue in state.ues},
            mcs_bounds={cell: state.mcs_bounds.get(cell, BASELINE_MCS_BOUNDS)
                        for cell in state.cells},
            attenuation={cell: state.tx_attenuation_db.get(
                cell, BASELINE_TX_ATTENUATION_DB) for cell in state.cells},
            quota=dict(state.slice_quota),
        )
        for axis, value in dict(configuration or {}).items():
            kind, target = _axis_parts(axis)
            if kind == AXIS_MCS_BOUNDS:
                applied.mcs_bounds[target] = str(value)
                continue
            if kind == AXIS_TX_ATTENUATION:
                attenuation = _as_float(value)
                if attenuation is not None:
                    applied.attenuation[target] = attenuation
                continue
            if kind == AXIS_SLICE_QUOTA:
                applied.quota[target] = str(value)
                continue
            if target not in applied.serving:
                continue
            if kind == AXIS_SERVING_CELL:
                applied.serving[target] = str(value)
            elif kind == AXIS_CAP:
                cap = _as_float(value)
                if cap is not None:
                    # the joint contracts encode "uncapped" as 0
                    applied.caps[target] = state.prb_total if cap <= 0 else cap
            elif kind == AXIS_PF_WEIGHT:
                weight = _as_float(value)
                if weight is not None and weight > 0:
                    applied.weights[target] = weight
        return applied

    def goodput(self, configuration: Mapping[str, Any],
                state: Optional[NetworkState] = None) -> Dict[str, float]:
        """Per-UE DL goodput in Mbps under this configuration."""
        state = state or self.state
        vector = self._read(configuration, state)
        serving, caps, weights = vector.serving, vector.caps, vector.weights
        applied = state.ues
        result = {ue: 0.0 for ue in state.ues}
        for cell in sorted(set(serving.values())):
            members = sorted(ue for ue in serving if serving[ue] == cell)
            # An MCS ceiling and a transmit attenuation are properties of the
            # *cell*: every UE on it loses the same fraction of its achievable
            # rate, so the honest place for them is the cell's capacity, not
            # one UE's ceiling.  Halving what the carrier can deliver halves
            # both contenders, and their PF split is unchanged -- which is
            # what makes lowering power a different move from capping one UE.
            capacity = self._capacity(cell) * vector.cell_factor(cell)
            if capacity <= 0 or not members:
                continue
            share_weight = {ue: max(0.0, weights[ue]) * self._efficiency(ue, cell)
                            for ue in members}
            total = sum(share_weight.values())
            if total <= 0:
                continue
            ceiling = {ue: max(0.0, min(
                state.offered_load_mbps.get(ue, capacity),
                caps[ue] / state.prb_total * capacity)) for ue in members}
            ceiling = self._clip_by_quota(ceiling, share_weight, capacity,
                                          state, vector)
            shares = {ue: min(ceiling[ue], capacity * share_weight[ue] / total)
                      for ue in members}
            leftover = max(0.0, capacity - sum(shares.values()))
            hungry = [ue for ue in members if shares[ue] < ceiling[ue] - 1e-9]
            denominator = sum(share_weight[ue] for ue in hungry)
            if leftover > 0 and denominator > 0:
                for ue in hungry:
                    shares[ue] = min(ceiling[ue],
                                     shares[ue] + leftover * share_weight[ue] / denominator)
            for ue in members:
                value = shares[ue]
                if applied.get(ue) and applied[ue] != cell and state.window_ms > 0:
                    # a cell change costs the transient out of the window
                    lost = min(1.0, self.handover_transient_ms / state.window_ms)
                    value *= max(0.0, 1.0 - lost)
                result[ue] = round(value, 6)
        return result

    def _clip_by_quota(self, ceiling: Dict[str, float],
                       share_weight: Mapping[str, float], capacity: float,
                       state: NetworkState, vector: "_Applied",
                       ) -> Dict[str, float]:
        """A slice's UEs on one cell may hold at most their quota of it.

        The cap is on the *group*, not on each member, so it is applied to the
        group's total and shared out by the same weight the cell is shared by.
        A UE in no slice, or in a slice with no quota applied, is untouched.
        """
        clipped = dict(ceiling)
        groups: Dict[str, List[str]] = {}
        for ue in ceiling:
            sst = state.ue_slices.get(ue)
            if sst:
                groups.setdefault(sst, []).append(ue)
        for sst, members in groups.items():
            share = _quota_share(vector.quota.get(sst, BASELINE_SLICE_QUOTA))
            if share >= 1.0:
                continue
            allowance = capacity * share
            wanted = sum(clipped[ue] for ue in members)
            if wanted <= allowance:
                continue
            total = sum(max(0.0, share_weight.get(ue, 0.0)) for ue in members)
            for ue in members:
                if total > 0:
                    clipped[ue] = allowance * max(0.0, share_weight.get(ue, 0.0)) / total
                else:
                    clipped[ue] = allowance / len(members)
        return clipped

    def deadline_success_ratio(self, configuration: Mapping[str, Any],
                               state: Optional[NetworkState] = None,
                               goodput: Optional[Mapping[str, float]] = None,
                               ) -> Dict[str, float]:
        """A coarse ``fraction completed within D`` per UE with an echo flow.

        Deliberately the crudest model that gets the **direction and rough
        size** right, which is all an estimate is for.  A tagged echo request
        costs :data:`DEFAULT_ECHO_PROCESSING_MS` at the far end plus the time
        to put :data:`DEFAULT_ECHO_PAYLOAD_BYTES` on the air at the share the
        UE is predicted to get; the queue it waits in behind its own bulk flow
        stretches that by ``1 / (1 - u)``, where ``u = max(0, 1 - share /
        offered)`` is the part of the UE's own offered load its share is not
        carrying.  The round trip is twice that, its spread is taken as
        exponential, and the fraction inside the deadline is
        ``1 - exp(-D / rtt)``.

        More share is less ``u`` and a shorter serialization, so more requests
        complete -- which is the only monotonicity the search needs from it.
        A cell change costs the transient out of the window exactly as the
        goodput does: those requests were issued while the UE was not being
        served and they never answered, so they are eligible and not completed.

        What it does **not** model: retransmissions, control-channel limits,
        the far end's own variance, and any correlation between consecutive
        requests.  The live path does not use it at all -- it observes.
        """
        state = state or self.state
        shares = dict(goodput if goodput is not None
                      else self.goodput(configuration, state))
        serving = self._read(configuration, state).serving
        applied = state.ues
        ratios: Dict[str, float] = {}
        for ue, deadline in state.echo_deadline_ms.items():
            if ue not in shares or deadline <= 0:
                continue
            share = max(0.0, float(shares[ue]))
            if share <= 0.0:
                ratios[ue] = 0.0
                continue
            serialization_ms = (DEFAULT_ECHO_PAYLOAD_BYTES * 8.0
                                / (share * 1e6) * 1000.0)
            offered = state.offered_load_mbps.get(ue, share)
            # How much of what the UE asked for it is **not** getting: nothing
            # when the share covers the offered load, approaching one as the
            # share collapses.  Written this way rather than as offered/share
            # because a UE asking for more than the cell can give would
            # otherwise pin the ratio at zero and make every control look
            # equally useless -- exactly the permanent open-loop overload the
            # scenario says to avoid reading as a RAN result.
            backlog = 0.0 if offered <= 0 else max(0.0, 1.0 - share / float(offered))
            one_way = ((DEFAULT_ECHO_PROCESSING_MS + serialization_ms)
                       / (1.0 - min(0.99, backlog)))
            rtt = 2.0 * one_way
            ratio = 1.0 - math.exp(-float(deadline) / rtt) if rtt > 0 else 0.0
            if (applied.get(ue) and applied[ue] != serving.get(ue, applied[ue])
                    and state.window_ms > 0):
                lost = min(1.0, self.handover_transient_ms / state.window_ms)
                ratio *= max(0.0, 1.0 - lost)
            ratios[ue] = round(min(1.0, max(0.0, ratio)), 6)
        return ratios

    def predict(self, configuration: Mapping[str, Any],
                network_state: Optional[NetworkState] = None,
                ) -> Dict[str, Tuple[Any, Any]]:
        """``{kpiKey: (value, uncertainty)}`` for one joint configuration."""
        state = network_state or self.state
        if isinstance(state, Mapping):
            state = NetworkState.from_record(state)
        serving = self._read(configuration, state).serving
        goodput = self.goodput(configuration, state)
        predicted: Dict[str, Tuple[Any, Any]] = {}
        for ue, value in goodput.items():
            key = kpi_key(KPI_GOODPUT, ue)
            if self._capacity(serving.get(ue, "")) <= 0:
                predicted[key] = (UNKNOWN, UNKNOWN)
                continue
            predicted[key] = (value, round(value * self._relative_uncertainty(ue), 6))
        for ue, cell in serving.items():
            predicted[kpi_key(KPI_SERVING_CELL, ue)] = (str(cell), 0.0)
        # Only for the UEs the sitting actually declared an echo flow on: a
        # ratio for a flow nobody is running would be a confident guess about
        # nothing.
        for ue, ratio in self.deadline_success_ratio(configuration, state,
                                                     goodput).items():
            key = kpi_key(KPI_DEADLINE_RATIO, ue)
            if self._capacity(serving.get(ue, "")) <= 0:
                predicted[key] = (UNKNOWN, UNKNOWN)
                continue
            predicted[key] = (ratio, round(ratio * self._relative_uncertainty(ue), 6))
        return predicted

    def _relative_uncertainty(self, ue: str) -> float:
        """Calibration narrows the band; it never claims certainty."""
        if not self.calibration_samples:
            return self.uncertainty
        return max(0.05, self.uncertainty / math.sqrt(1.0 + self.calibration_samples))

    # -- the evidence table ------------------------------------------------- #

    def prediction_table(self, action_space: Mapping[str, Sequence[Any]],
                         baselines: Optional[Mapping[str, Any]] = None,
                         limit: int = 64,
                         extra: Sequence[Mapping[str, Any]] = (),
                         ) -> List[Dict[str, Any]]:
        """``effect_evidence.predictions``: the product when it fits in
        ``limit`` rows, otherwise the baseline, every single-axis move and the
        joint moves that fit (contract v2 section 4)."""
        space = {str(axis): [str(value) for value in values]
                 for axis, values in dict(action_space or {}).items()}
        baseline = {str(axis): str(value) for axis, value in dict(baselines or {}).items()}
        for axis, values in space.items():
            baseline.setdefault(axis, values[0] if values else "")
        limit = max(1, int(limit))

        product = 1
        for values in space.values():
            product *= max(1, len(values))
        rows: List[Dict[str, str]] = []
        if product <= limit:
            axes = sorted(space)
            for combination in itertools.product(*(space[axis] for axis in axes)):
                rows.append(dict(zip(axes, combination)))
        else:
            rows.append(dict(baseline))
            for axis in sorted(space):
                for value in space[axis]:
                    if value == baseline.get(axis):
                        continue
                    row = dict(baseline)
                    row[axis] = value
                    rows.append(row)
            for item in extra:
                row = dict(baseline)
                row.update({str(k): str(v) for k, v in dict(item).items()
                            if str(k) in space})
                rows.append(row)
            for left_axis, right_axis in itertools.combinations(sorted(space), 2):
                for left in space[left_axis]:
                    for right in space[right_axis]:
                        if (left == baseline.get(left_axis)
                                and right == baseline.get(right_axis)):
                            continue
                        row = dict(baseline)
                        row[left_axis], row[right_axis] = left, right
                        rows.append(row)

        table: List[Dict[str, Any]] = []
        seen = set()
        for row in rows:
            signature = tuple(sorted(row.items()))
            if signature in seen:
                continue
            seen.add(signature)
            predicted = self.predict(row)
            table.append({
                "configuration": dict(row),
                "predicted": {key: value for key, (value, _u) in predicted.items()},
                "uncertainty": {key: unc for key, (_v, unc) in predicted.items()}})
            if len(table) >= limit:
                break
        return table

    def describe(self) -> Dict[str, Any]:
        """``effect_evidence.predictorDescription``: the model form and where
        its numbers currently come from."""
        return {
            "form": ("analytic capacity share: per-cell capacity divided by PF "
                     "weight times link efficiency, each UE clipped by its "
                     "offered load and by its PRB cap as a fraction of the "
                     "carrier, one weighted redistribution pass, and a "
                     "handover transient charged against the observation "
                     "window; a cell's MCS ceiling and transmit attenuation "
                     "clip every UE on that cell, and a slice quota clips the "
                     "UEs of that slice together"),
            "cellEffects": (
                "dlMcsBounds: the ceiling's 38.214 spectral efficiency over the "
                "unconstrained ceiling's, a rate ceiling only -- the floor "
                "costs reliability, which this model does not estimate. "
                f"txAttenuationDb: charged against a nominal "
                f"{DEFAULT_NOMINAL_SNR_DB:g} dB SNR, rate as log2(1+SNR). "
                "slicePrbQuota: maxPrbPolicyRatio as the group's share of the "
                "cell; min and dedicated are not modelled"),
            "deadlineSuccess": (
                "deadlineSuccessRatio: 1 - exp(-D / rtt) with rtt twice "
                f"({DEFAULT_ECHO_PROCESSING_MS:g} ms + the time to serialize "
                f"{DEFAULT_ECHO_PAYLOAD_BYTES:g} bytes at the predicted share), "
                "stretched by 1/(1-u) for the part u of the UE's offered load "
                "its share is not carrying, and multiplied by the served "
                "fraction of the window "
                "after a cell change.  Retransmissions, control-channel limits "
                "and request-to-request correlation are not modelled; only the "
                "UEs the state names an echoDeadlineMs for get a ratio at all"),
            "echoDeadlineMs": dict(self.state.echo_deadline_ms),
            "cells": {cell: round(self._capacity(cell), 6) for cell in self.state.cells},
            "prbTotal": self.state.prb_total,
            "nominalSnrDb": DEFAULT_NOMINAL_SNR_DB,
            "handoverTransientMs": self.handover_transient_ms,
            "relativeUncertainty": self.uncertainty,
            "calibration": {
                "samples": self.calibration_samples,
                "state": "calibrated" if self.calibration_samples else "uncalibrated",
                "capacityScale": dict(self.capacity_scale),
                "efficiencyScale": dict(self.efficiency_scale)},
            "note": ("estimates only; the Kernel judges measured KPIs and this "
                     "model never issues a verdict"),
        }

    # -- calibration -------------------------------------------------------- #

    def calibrate(self, observations: Iterable[Mapping[str, Any]], *,
                  rate: float = 0.5) -> "JointEffectPredictor":
        """Move the per-cell capacity and the per-UE efficiency toward what was
        actually observed.

        One damped step per call (``rate``), so a single noisy window cannot
        take the model over, and only from observations that carry both a
        configuration and a goodput.  Returns ``self`` so the executor can
        calibrate and predict in one line.
        """
        rows: List[Tuple[Dict[str, Any], Dict[str, float]]] = []
        for item in observations or ():
            record = dict(item or {})
            if record.get("valid") is False:
                continue
            configuration = dict(record.get("configuration") or {})
            kpis = dict(record.get("kpis") or {})
            measured: Dict[str, float] = {}
            for key, value in kpis.items():
                kind, ue = _axis_parts(key)
                number = _as_float(value)
                if kind == KPI_GOODPUT and ue and number is not None:
                    measured[ue] = number
            if measured:
                rows.append((configuration, measured))
        if not rows:
            return self

        cell_ratios: Dict[str, List[float]] = {}
        ue_ratios: Dict[str, List[float]] = {}
        for configuration, measured in rows:
            serving = self._read(configuration, self.state).serving
            predicted = self.goodput(configuration, self.state)
            by_cell: Dict[str, Tuple[float, float]] = {}
            for ue, observed in measured.items():
                expected = predicted.get(ue)
                if expected is None:
                    continue
                cell = serving.get(ue, "")
                seen, want = by_cell.get(cell, (0.0, 0.0))
                by_cell[cell] = (seen + observed, want + expected)
                if expected > 1e-6:
                    ue_ratios.setdefault(ue, []).append(observed / expected)
            for cell, (observed_sum, expected_sum) in by_cell.items():
                if expected_sum > 1e-6 and self._capacity(cell) > 0:
                    cell_ratios.setdefault(cell, []).append(observed_sum / expected_sum)

        rate = min(1.0, max(0.0, float(rate)))
        for cell, ratios in cell_ratios.items():
            ratio = sum(ratios) / len(ratios)
            scale = self.capacity_scale.get(cell, 1.0)
            self.capacity_scale[cell] = round(scale * (1.0 + rate * (ratio - 1.0)), 6)
        for ue, ratios in ue_ratios.items():
            ratio = sum(ratios) / len(ratios)
            cells = {self.state.ues.get(ue, "")}
            correction = 1.0
            for cell in cells:
                correction = self.capacity_scale.get(cell, 1.0) or 1.0
            residual = ratio / correction if correction else ratio
            scale = self.efficiency_scale.get(ue, 1.0)
            self.efficiency_scale[ue] = round(
                max(0.05, scale * (1.0 + rate * (residual - 1.0))), 6)
        self.calibration_samples += len(rows)
        return self

    # -- what the executor hands to a model --------------------------------- #

    def effect_evidence(self, action_space: Mapping[str, Sequence[Any]],
                        baselines: Optional[Mapping[str, Any]] = None,
                        observations: Sequence[Mapping[str, Any]] = (),
                        limit: int = 64) -> Dict[str, Any]:
        """``input.effect_evidence`` of ``SINGLE_CALL.md``."""
        return {
            "predictorDescription": self.describe(),
            "predictions": self.prediction_table(action_space, baselines, limit),
            "observations": [dict(item) for item in observations],
            "uncertaintyNote": ("relative uncertainty on every goodput; a KPI "
                                "the model has no basis for is 'unknown'"),
        }
