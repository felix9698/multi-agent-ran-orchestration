"""Seeded RAN observations and injected ports for the joint Kernel runtime.

All observations are emulator evidence in a MOCK session, never OTA evidence.
The existing joint contracts encode uncapped PRBs as 0 and PF weights as a
ratio (neutral 1.0); adapters translate those to 24 PRBs and weight 8.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, List, Mapping, Protocol, Sequence

from oran.rapp.contract_support import load_schema

from assurance.gateway.mock_adapter import MockActuationAdapter
from assurance.objectives.action102_support import SUPPLEMENTARY_ACTIONS
from assurance.objectives.joint import (
    CapAxisSpec, IntentSpec, PriorityAxisSpec, SteeringAxisSpec, compose_joint,
    McsBoundsAxisSpec, TxAttenuationAxisSpec, SlicePrbQuotaAxisSpec,
)
from tools.hfconsole.build import _VirtualClock

POLICY_TYPE_ID = "AIC_UECellSteering_1.0.0"
CONDITIONS = {"light-load": 1.5, "contention-boundary": 4.0,
              "relaxable-multiservice": 5.0}

#: Scenario I4's tagged echo flow.  A command echo is a small datagram issued
#: at a steady rate; the same coarse constants
#: :mod:`assurance.coordination.predictor` estimates with, so the emulator and
#: the predictor disagree only about the *state*, never about the units.
ECHO_PAYLOAD_BYTES = 200.0
ECHO_PROCESSING_MS = 5.0
DEFAULT_ECHO_RATE_HZ = 10.0


def _node(nb_id: int) -> str:
    return f"ngran=02;plmn=208-095-2;nb={nb_id:010d}/00;cudu=none:00000000000000000000"


def _nodes(cells: Sequence[str]) -> dict[str, int]:
    return {str(cell): 0xE00 - index * 0x300 for index, cell in enumerate(cells)}


class EmulatedRan:
    """N UEs sharing cell capacity with one weighted redistribution pass.

    ``ues`` maps UE ids to serving NCIs; ``cells`` maps NCIs to Mbps.
    Link efficiencies are relative, per UE and cell. MCS and attenuation
    scale each cell's achievable capacity; slice maxima cap aggregate shares
    per cell (minimum/dedicated ratios are read back, not reservation promises).
    ``ue_slices`` maps UE ids to SST strings, defaulting to slice "1".
    Repeated reads at one instant share noise, independent of poll count.

    A **tagged echo flow** (scenario I4) is off until
    :meth:`enable_tagged_echo` turns it on, so a sitting that never declares a
    deadline sees exactly the KPI vector it always did.  Once on, the flow
    issues at a stated rate per UE and every request's round trip follows the
    same capacity model the goodput does; the counters it keeps are what
    ``deadlineSuccessRatio`` is differenced out of.
    """

    def __init__(self, *, ues: Mapping[str, str], cells: Mapping[str, float],
                 offered_load_mbps: Mapping[str, float] | None = None,
                 link_efficiency: Mapping[str, Mapping[str, float]] | None = None,
                 ue_slices: Mapping[str, str] | None = None,
                 seed: int = 0, noise_sigma: float = 0.03,
                 handover_transient_ms: int = 2000,
                 echo_deadline_ms: Mapping[str, float] | float | None = None,
                 echo_rate_hz: float = DEFAULT_ECHO_RATE_HZ) -> None:
        self.cells = {str(k): float(v) for k, v in cells.items()}
        self.ues = {str(k): str(v) for k, v in ues.items()}
        self.offered_load_mbps = {ue: float((offered_load_mbps or {}).get(ue, 4.0))
                                  for ue in self.ues}
        self.link_efficiency = {
            ue: {cell: float((link_efficiency or {}).get(ue, {}).get(cell, 1.0))
                 for cell in self.cells} for ue in self.ues}
        self.caps = {ue: 24.0 for ue in self.ues}
        self.pf_weights = {ue: 8.0 for ue in self.ues}
        self.ue_slices = {ue: str((ue_slices or {}).get(ue, "1")) for ue in self.ues}
        self.mcs_bounds = {cell: (0, 28) for cell in self.cells}
        self.tx_attenuation_db = {cell: 0.0 for cell in self.cells}
        self.slice_quotas = {sst: (0, 1, 100) for sst in self.ue_slices.values()}
        self.seed, self.noise_sigma = int(seed), float(noise_sigma)
        self.handover_transient_ms = int(handover_transient_ms)
        self.ms = 0
        self._unavailable_until = {ue: 0 for ue in self.ues}
        #: UE -> D1 in ms, for the UEs a tagged echo flow is running on.
        self.echo_deadline_ms: dict[str, float] = {}
        self.echo_rate_hz = float(echo_rate_hz)
        #: UE -> cumulative ``{"issued", "eligible", "completed"}``.
        self.echo_counters: dict[str, dict[str, int]] = {}
        #: UE -> the requests issued and not yet past their deadline, as
        #: ``(due_ms, completed)`` -- a request joins the denominator only once
        #: its deadline has passed, answered or not.
        self._echo_pending: dict[str, list[tuple[float, bool]]] = {}
        #: UE -> how many requests the flow has issued, so a request's issue
        #: time and its seeded jitter are both a function of its index alone.
        self._echo_issued_count: dict[str, int] = {}
        if echo_deadline_ms is not None:
            self.enable_tagged_echo(echo_deadline_ms, rate_hz=echo_rate_hz)
        if any(cell not in self.cells for cell in self.ues.values()):
            raise ValueError("each UE needs a known serving cell")
        if any(value < 0 for value in self.cells.values()) or noise_sigma < 0:
            raise ValueError("capacity and noise must be nonnegative")

    def serving_cell(self, ue: str) -> str:
        return self.ues[str(ue)]

    def advance(self, ms: int) -> None:
        start, self.ms = self.ms, self.ms + max(0, int(ms))
        if self.echo_deadline_ms and self.ms > start:
            self._advance_tagged_echo(float(start), float(self.ms))

    # -- the tagged echo flow (scenario I4) --------------------------------- #

    def enable_tagged_echo(self, deadline_ms: Mapping[str, float] | float,
                           *, ues: Sequence[str] | None = None,
                           rate_hz: float | None = None) -> None:
        """Run a tagged echo flow on these UEs, measured within ``deadline_ms``.

        One number applies the same deadline to ``ues`` (every UE by default);
        a mapping names a deadline per UE.  The counters start at zero and the
        flow is deterministic under the emulator's existing seed, so two runs
        of the same sitting produce the same ratio.
        """
        if rate_hz is not None:
            self.echo_rate_hz = float(rate_hz)
        if self.echo_rate_hz <= 0:
            raise ValueError("a tagged echo flow needs a positive issue rate")
        if isinstance(deadline_ms, Mapping):
            wanted = {str(ue): float(value) for ue, value in deadline_ms.items()}
        else:
            wanted = {str(ue): float(deadline_ms)
                      for ue in (ues if ues is not None else self.ues)}
        for ue, deadline in wanted.items():
            if ue not in self.ues:
                raise ValueError(f"unknown UE {ue}")
            if deadline <= 0:
                raise ValueError("a deadline is a positive number of milliseconds")
            self.echo_deadline_ms[ue] = deadline
            self.echo_counters.setdefault(
                ue, {"issued": 0, "eligible": 0, "completed": 0})
            self._echo_pending.setdefault(ue, [])
            self._echo_issued_count.setdefault(ue, 0)

    def _advance_tagged_echo(self, start: float, end: float) -> None:
        """Issue, serve and retire the requests of ``(start, end]``.

        Every request is served at the share the goodput model reports **at its
        own issue time**, not at the step's end, so a flow advanced in one
        30-second step and the same flow advanced in twelve 2.5-second steps
        produce the same counters: the metric is a property of the emulated
        radio, not of how often the executor happened to poll it.  A request
        issued while the UE is inside its handover transient is never served
        and never answers -- it counts in the denominator, which is the whole
        point of the metric.
        """
        period = 1000.0 / self.echo_rate_hz
        for ue, deadline in sorted(self.echo_deadline_ms.items()):
            counters = self.echo_counters[ue]
            pending = self._echo_pending[ue]
            # Request k is issued at ``k * period``, so the count already
            # issued is exactly where this step picks up: no request is issued
            # twice and none is skipped, however the caller chops up the clock.
            index = self._echo_issued_count[ue]
            while index * period < end - 1e-9:
                issued_at = index * period
                counters["issued"] += 1
                pending.append((issued_at + deadline,
                                self._echo_completes(ue, index, issued_at,
                                                     self._share_at(ue, issued_at),
                                                     deadline)))
                index += 1
            self._echo_issued_count[ue] = index
            # A request is eligible once its deadline has passed, whether or
            # not it answered; until then it is neither a success nor a miss.
            still_pending = []
            for due, completed in pending:
                if due <= end + 1e-9:
                    counters["eligible"] += 1
                    counters["completed"] += 1 if completed else 0
                else:
                    still_pending.append((due, completed))
            self._echo_pending[ue] = still_pending

    def _share_at(self, ue: str, at_ms: float) -> float:
        """The UE's goodput share at one instant, read out of the same model.

        The clock is moved to that instant for the read and put straight back:
        ``dl_goodput`` already keys its noise and its handover mask off
        ``self.ms``, so reading it this way is exactly the share the emulator
        would have reported had it been polled then.
        """
        now = self.ms
        try:
            self.ms = at_ms
            return float(self.dl_goodput().get(ue, 0.0))
        finally:
            self.ms = now

    def _echo_completes(self, ue: str, index: int, issued_at: float,
                        share_mbps: float, deadline_ms: float) -> bool:
        """Did request ``index`` come back inside ``deadline_ms``?

        The service time is the same capacity model the goodput uses -- the
        payload put on the air at the UE's current share -- plus a fixed
        processing constant, stretched by the part of the UE's offered load its
        share is not carrying, and doubled for the round trip.  That is the
        request's **mean**; the request itself draws an exponential around it,
        which is what gives the ratio a gradient instead of a step at the
        deadline and is the same distribution
        :meth:`assurance.coordination.predictor.JointEffectPredictor.deadline_success_ratio`
        estimates with.  A request issued during the handover transient never
        comes back at all.
        """
        if issued_at < self._unavailable_until[ue]:
            return False
        if share_mbps <= 0:
            return False
        serialization = ECHO_PAYLOAD_BYTES * 8.0 / (share_mbps * 1e6) * 1000.0
        offered = self.offered_load_mbps.get(ue, share_mbps)
        backlog = 0.0 if offered <= 0 else max(0.0, 1.0 - share_mbps / offered)
        one_way = (ECHO_PROCESSING_MS + serialization) / (1.0 - min(0.99, backlog))
        draw = random.Random(f"{self.seed}:echo:{ue}:{index}").random()
        return 2.0 * one_way * -math.log(max(1e-12, 1.0 - draw)) <= deadline_ms

    def echo_ratio(self, ue: str) -> float | None:
        """The cumulative ratio, for a caller that wants it without a window."""
        counters = self.echo_counters.get(str(ue))
        if not counters or counters["eligible"] <= 0:
            return None
        return counters["completed"] / counters["eligible"]

    def apply(self, axis: str, ue: str, value: Any) -> None:
        ue, axis = str(ue), axis.split("@", 1)[0]
        if axis in {"dlMcsBounds", "txAttenuationDb", "slicePrbQuota"}:
            parameters = self.action_parameters(axis, ue, value)
            if axis == "dlMcsBounds":
                self.mcs_bounds[ue] = (parameters["minDlMcs"], parameters["maxDlMcs"])
            elif axis == "txAttenuationDb":
                self.tx_attenuation_db[ue] = parameters["txAttenuationDb"]
            else:
                self.slice_quotas[ue] = tuple(parameters[key] for key in (
                    "dedicatedPrbPolicyRatio", "minPrbPolicyRatio", "maxPrbPolicyRatio"))
            return
        self.ues[ue]  # unknown UE is an input error
        if axis == "servingCell":
            cell = str(value)
            if cell not in self.cells:
                raise ValueError(f"unknown cell {cell}")
            if self.ues[ue] != cell:
                self.ues[ue] = cell
                self._unavailable_until[ue] = self.ms + self.handover_transient_ms
        elif axis == "dlPrbCap":
            cap = float(value)
            if not 0 <= cap <= 24:
                raise ValueError("cap must be between 0 and 24 PRBs")
            self.caps[ue] = 24.0 if cap == 0 else cap
        elif axis == "pfWeight":
            weight = float(value)
            if weight <= 0:
                raise ValueError("PF weight must be positive")
            self.pf_weights[ue] = weight
        else:
            raise ValueError(f"unknown axis {axis}")

    def action_parameters(self, axis: str, scope: str, value: Any) -> dict[str, Any]:
        """Decode a composite axis using the shared supplementary action rules."""
        kind = axis.split("@", 1)[0]
        if kind in {"dlMcsBounds", "txAttenuationDb"}:
            if scope not in self.cells:
                raise ValueError(f"unknown cell {scope}")
        elif scope not in self.slice_quotas:
            raise ValueError(f"unknown slice {scope}")
        action = next(action for action in SUPPLEMENTARY_ACTIONS.values() if action.axis == kind)
        return action.policy_values(str(value))

    def axis_value(self, axis: str) -> str:
        """Current encoded state, including cell and slice composite values."""
        kind, scope = axis.split("@", 1)
        action = next((action for action in SUPPLEMENTARY_ACTIONS.values()
                       if action.axis == kind), None)
        if kind == "dlMcsBounds":
            minimum, maximum = self.mcs_bounds[scope]
            return action.axis_value_text({"minDlMcs": minimum, "maxDlMcs": maximum})
        if kind == "txAttenuationDb":
            return action.axis_value_text({"txAttenuationDb": self.tx_attenuation_db[scope]})
        if kind == "slicePrbQuota":
            dedicated, minimum, maximum = self.slice_quotas[scope]
            return action.axis_value_text({"sst": int(scope), "sd": "ffffff",
                "dedicatedPrbPolicyRatio": dedicated, "minPrbPolicyRatio": minimum,
                "maxPrbPolicyRatio": maximum})
        if kind == "servingCell":
            return self.serving_cell(scope)
        return str({"dlPrbCap": self.caps, "pfWeight": self.pf_weights}[kind][scope])

    def dl_goodput(self) -> dict[str, float]:
        result = {ue: 0.0 for ue in self.ues}
        for cell, capacity in self.cells.items():
            # Coarse rate ceilings, not a link-level MCS/reliability simulator.
            minimum, maximum = self.mcs_bounds[cell]
            capacity *= (maximum + 1) / 29 * (1 - minimum / 58)
            capacity *= 10 ** (-self.tx_attenuation_db[cell] / 20)
            members = [ue for ue in sorted(self.ues) if self.ues[ue] == cell
                       and self.ms >= self._unavailable_until[ue]]
            weights = {ue: self.pf_weights[ue] * self.link_efficiency[ue][cell]
                       for ue in members}
            total = sum(weights.values())
            if total <= 0:
                continue
            limits = {ue: max(0.0, min(self.offered_load_mbps[ue],
                                      self.caps[ue] / 24 * capacity)) for ue in members}
            shares = {ue: min(limits[ue], capacity * weights[ue] / total) for ue in members}
            remaining = max(0.0, capacity - sum(shares.values()))
            eligible = [ue for ue in members if shares[ue] < limits[ue]]
            denominator = sum(weights[ue] for ue in eligible)
            if denominator > 0:
                for ue in eligible:
                    shares[ue] = min(limits[ue], shares[ue] + remaining * weights[ue] / denominator)
            # A quota limits the aggregate PRB share of a slice on each cell.
            # Redistribute released capacity only to slices with quota headroom.
            groups = {sst: [u for u in members if self.ue_slices[u] == sst]
                      for sst in self.slice_quotas}
            quota_released = False
            for sst, group in groups.items():
                used = sum(shares[u] for u in group)
                ceiling = capacity * self.slice_quotas[sst][2] / 100
                if used > ceiling:
                    quota_released = True
                    for ue in group:
                        shares[ue] *= ceiling / used
            remaining = max(0.0, capacity - sum(shares.values()))
            eligible = [u for u in members if shares[u] < limits[u]
                        and sum(shares[v] for v in groups[self.ue_slices[u]])
                        < capacity * self.slice_quotas[self.ue_slices[u]][2] / 100]
            denominator = sum(weights[u] for u in eligible)
            additions = {u: min(limits[u] - shares[u], remaining * weights[u] / denominator)
                         for u in eligible} if denominator and quota_released else {}
            for sst, group in groups.items():
                extra = sum(additions.get(u, 0) for u in group)
                headroom = max(0.0, capacity * self.slice_quotas[sst][2] / 100
                               - sum(shares[u] for u in group))
                scale = min(1.0, headroom / extra) if extra else 1.0
                for ue in group:
                    shares[ue] += additions.get(ue, 0) * scale
            for ue in members:
                noise = random.Random(f"{self.seed}:{self.ms}:{ue}").gauss(0, self.noise_sigma)
                shares[ue] = min(limits[ue], max(0.0, shares[ue] * (1 + noise)))
            for sst, group in groups.items():
                used = sum(shares[u] for u in group)
                ceiling = capacity * self.slice_quotas[sst][2] / 100
                if used > ceiling:
                    for ue in group:
                        shares[ue] *= ceiling / used
            # Measurement variation must not create extra cell capacity.
            scale = min(1.0, capacity / sum(shares.values())) if sum(shares.values()) else 1.0
            result.update({ue: value * scale for ue, value in shares.items()})
        return result


def three_ue_topology(*, condition: str = "contention-boundary", seed: int = 0,
                      noise_sigma: float = 0.03, **kwargs: Any) -> EmulatedRan:
    """Scenario preset; conditions change offered traffic, never targets.

    ``echo_deadline_ms=`` turns on I4's tagged echo flow -- a number for every
    UE, or ``{"131": 50.0}`` for the one UE the scenario runs it on.
    """
    load = CONDITIONS[condition]
    return EmulatedRan(ues={"131": "12345678", "132": "12345678", "133": "87654321"},
                       cells={"12345678": 5.0, "87654321": 5.0},
                       offered_load_mbps={ue: load for ue in ("131", "132", "133")},
                       seed=seed, noise_sigma=noise_sigma, **kwargs)


class EmulatedClock(_VirtualClock):
    def __init__(self, ran: EmulatedRan) -> None:
        super().__init__()
        self.ran = ran
        self.ms = ran.ms

    def sleep_ms(self, ms: int) -> None:
        self.ran.advance(ms)
        self.ms = self.ran.ms


class EmulatedKpmStream:
    """Format-3 attribution published by each UE's current serving node."""
    def __init__(self, clock: Any, ran: EmulatedRan, epoch: int = 272) -> None:
        self.clock, self.ran, self.epoch = clock, ran, int(epoch)
        self.publishing = True
        self.nodes = _nodes(tuple(ran.cells))
        #: role label -> the AMF UE NGAP id the network gives it now; a numeric UE is its own id.
        self.amf_of: dict[str, int] = {}
        # A role-named UE (ue1/ue2/ue3) has no numeric id of its own, and the
        # live path gets one from KPM.  Nothing seeds one here, so ``int(ue)``
        # below raised and **no role-keyed sitting could be emulated at all**
        # (2026-09-17: noradio_construction_check.py had not been runnable).
        # Assign deterministically, in the RAN's own UE order, and only for
        # names that are not already numeric.  An explicit ``None`` still means
        # "not registered now" and is left alone.
        for offset, ue in enumerate(ran.ues):
            if not str(ue).isdigit():
                self.amf_of[ue] = 900001 + offset

    def __call__(self) -> Sequence[str]:
        if not self.publishing:
            return ()
        received = datetime.fromisoformat(self.clock.now().replace("Z", "+00:00"))
        return tuple(json.dumps({
            "event": "kpm_indication", "kpm_msg_format": 3,
            "e2_node": _node(self.nodes[cell]), "nb_id": self.nodes[cell],
            "connection_epoch": self.epoch,
            "recv_unix_us": int(received.timestamp() * 1_000_000),
            "ues": [{"amf_ue_ngap_id": int(self.amf_of.get(ue, ue)), "guami": {
                "mcc": 208, "mnc": 95, "mnc_digit_len": 2,
                "amf_region_id": 1, "amf_set_id": 64, "amf_pointer": 4}}],
        }) for ue, cell in self.ran.ues.items()
            if self.amf_of.get(ue, ue) is not None)  # None: the UE is not registered now


class EmulatedActuationAdapter(MockActuationAdapter):
    """Mock gateway semantics, including lost ACKs and undo, backed by RAN state.

    ``pf_ratio=True`` is for existing PriorityAxisSpec's ratio-valued contract;
    direct physical PF weights use the default ``False``.
    """
    hosts_watchdogs = False

    def __init__(self, ran: EmulatedRan, axis: str, *, baseline: Any = None,
                 name: str | None = None, pf_ratio: bool = False, **kwargs: Any) -> None:
        kind, ue = axis.split("@", 1)
        self.ran, self.axis, self.ue, self.pf_ratio = ran, axis, ue, pf_ratio
        if baseline is None:
            baseline = ran.axis_value(axis)
        key = {"servingCell": "r1-steer", "dlPrbCap": "r1-cap", "pfWeight": "r1-pf",
               **{action.axis: action.adapter for action in SUPPLEMENTARY_ACTIONS.values()
                  if action.axis in {"dlMcsBounds", "txAttenuationDb", "slicePrbQuota"}}}[kind]
        super().__init__(config={axis: str(baseline)}, name=name or f"{key}@{ue}", **kwargs)

    def _write(self, command: Mapping[str, Any], reference: str) -> Any:
        if self.axis.split("@", 1)[0] in {"dlMcsBounds", "txAttenuationDb", "slicePrbQuota"}:
            if command["axis"] != self.axis:
                raise ValueError(f"adapter {self.name} does not own {command['axis']}")
            self.ran.action_parameters(self.axis, self.ue, command["value"])
        before = len(self.writes)
        result = super()._write(command, reference)
        if len(self.writes) > before:
            value = command["value"]
            if self.pf_ratio:
                value = 8 * float(value)
            self.ran.apply(self.axis, self.ue, value)
        return result


class KpiObserver(Protocol):
    """The same sampling interface used by MOCK and LIVE sitting executors."""
    def sample(self) -> dict[str, Any]: ...


class EmulatedKpiObserver:
    """The emulator's KPI vector, in the shape the live observer answers in.

    The tagged echo flow contributes ``deadlineSuccessRatio@<ue>`` as the raw
    **counters** rather than as a ratio, because the window's value is
    ``completed / eligible`` over the whole window and a mean of per-sample
    ratios is a different (wrong) number.  The observation rule differences
    them; the counters are cumulative and monotone, so any window can be asked
    for.  A sitting with no echo flow gets exactly the vector it always got.
    """

    def __init__(self, ran: EmulatedRan) -> None:
        self.ran = ran

    def sample(self) -> dict[str, Any]:
        return {**{f"dlGoodputMbps@{ue}": value for ue, value in self.ran.dl_goodput().items()},
                **{f"servingCell@{ue}": cell for ue, cell in self.ran.ues.items()},
                **{f"deadlineSuccessRatio@{ue}": dict(counters)
                   for ue, counters in sorted(self.ran.echo_counters.items())
                   if ue in self.ran.echo_deadline_ms}}


def _schema_shaped_value(key: str, secret: Optional[Path] = None) -> Any:
    """A value the integration-values schema accepts for *key*.

    The same shape rules ``tests/test_integration_values.py`` uses to build a
    valid document without a deployment; only the handful of keys this
    composition root reads are then overwritten with the generated fixture.
    """
    if key.endswith("Sha256"):
        return "a" * 64
    if key.endswith("Path"):
        return "artifacts/value.json"
    if key.endswith("Ref"):
        # file:// naming a real regular file, not env://: the R1 security check
        # refuses the env:// vocabulary outright (resolving it is a deployment
        # secret-store concern this release does not implement) and then
        # insists the file:// target is a regular file.  A hermetic profile
        # that satisfies neither makes the loader refuse the fixture rather
        # than a deployment, which says nothing about the sitting under test.
        return f"file://{secret}" if secret is not None else "file:///dev/null"
    if key == "o1.netconf.endpoint":
        return "ssh://netconf.example.test:830"
    if key == "o1.sftp.allowedAuthorities":
        return ["sftp.example.test:22"]
    if key.endswith("Dn"):
        return "ManagedElement=example"
    if key == "o1.fileDataReporting.mnsVersion":
        return "v1"
    if key.endswith("ClientId") or key == "r1.rAppId":
        return "client-1"
    if key.endswith("TokenEndpoint") or key.endswith("managedObjectUri"):
        return "https://auth.example.test/token"
    if (key.endswith("apiRoot") or key.endswith("BaseUri")
            or key.endswith("Destination") or key.endswith("mnsRoot")
            or key.endswith("consumerReference")):
        return "https://service.example.test/api"
    return "integration-value"



class EmulatedPolicyPort:
    """An in-process stand-in for the R1 consumer.  Opens nothing.

    It answers policy-type discovery with this repository's own pinned schema,
    so :func:`tools.g3ota.composition.build_policy_type_discovery` takes its
    inline-schema branch and never has to resolve a digest.
    """

    def __init__(self, policy_type_id: str) -> None:
        self.policy_type_id = policy_type_id
        self.calls: List[str] = []

    def bootstrap_info(self) -> Mapping[str, Any]:
        self.calls.append("bootstrap_info")
        return {"apiRoot": "in-process"}

    def discover_services(self, **_kwargs: Any) -> Sequence[Any]:
        self.calls.append("discover_services")
        return ()

    def discover_policy_types(self) -> Sequence[str]:
        self.calls.append("discover_policy_types")
        return [self.policy_type_id]

    def get_policy_type(self, policy_type_id: str) -> Mapping[str, Any]:
        self.calls.append(f"get_policy_type:{policy_type_id}")
        return {"policyTypeId": policy_type_id,
                "policySchema": load_schema(f"{policy_type_id}.policy"),
                "statusSchema": load_schema(f"{policy_type_id}.status")}




class HermeticDeployment:
    """Generate the LiveConsoleFixture document shapes without any lab inputs."""
    @staticmethod
    def write(tmp_dir: Any, *, ues: Mapping[str, str], cells: Mapping[str, float],
              epoch: int = 272) -> Path:
        from oran.contract.integration_values import SCHEMA_NAME
        from oran.contract.validator import ContractValidator
        directory = Path(tmp_dir).resolve()
        directory.mkdir(parents=True, exist_ok=True)

        def write(name: str, value: Any) -> Path:
            path = directory / name
            path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return path

        nodes = _nodes(tuple(cells))
        capability = write("capability.json", {
            "schemaDigests": {"policy": "a" * 64, "status": "b" * 64},
            "topology": {"cells": [{
                "cellId": {"cId": {"ncI": int(cell)}, "plmnId": {"mcc": "208", "mnc": "95"}},
                "globalE2NodeId": {"nodeId": {"bitLength": 32, "hex": f"0x{nb:08x}"},
                                   "nodeType": "GNB"}} for cell, nb in nodes.items()]}})
        required = ContractValidator().schema(SCHEMA_NAME)["properties"]["values"]["required"]
        secret = directory / "hermetic-secret.pem"
        secret.write_text("hermetic fixture material, never a credential\n", encoding="utf-8")
        values = {key: _schema_shaped_value(key, secret) for key in required}
        r1_root, a1_root = "https://r1.emulated.test:18443/r1", "https://a1p.emulated.test:9444/A1-P/v2"
        values.update({"r1.apiRoot": r1_root, "a1.apiRoot": a1_root,
                       "backend.capabilityManifestPath": capability.name,
                       "backend.capabilityManifestSha256": hashlib.sha256(capability.read_bytes()).hexdigest()})
        integration = write("integration-values.json", {
            "schemaVersion": "oran-aic-integration-values/1.0.0", "contractProfile": "oran-aic/1.0.0",
            "deploymentMode": "MERGED", "bundleManifestJcsSha256":
                "6f9908ca9cee29ca5fa7b629f4daa0f502b5b8ce244519a76b9c88c6c1710ce3", "values": values})
        refs = {key: f"file://{directory}/unused-secret" for key in (
            "mtlsCa", "mtlsClientCertificate", "mtlsClientPrivateKey", "oauthToken")}
        binding = write("binding.json", {
            "schemaVersion": "assurance-live-binding/1.0.0", "bindingId": "emulated-ran",
            "sources": [{"path": str(integration), "sha256": hashlib.sha256(integration.read_bytes()).hexdigest()}],
            "r1": {"apiRoot": r1_root, "nearRtRicId": "emulated-ric", "policyTypeId": POLICY_TYPE_ID,
                   "secretRefs": refs, "polling": {"cadenceMs": 1000, "deadlineMs": 20000}},
            "a1p": {"apiRoot": a1_root, "secretRefs": refs},
            "o1": {"httpsRoot": "https://o1.emulated.test:8443/o1", "netconf": "ssh://o1.emulated.test:830",
                   "sftp": "sftp://o1.emulated.test:2022/pm", "pmDirectory": str(directory / "pm"), "secretRefs": refs},
            "kpm": {"jsonlPath": str(directory / "kpm.jsonl"),
                    "expectedEpochs": {_node(nb): int(epoch) for nb in nodes.values()}},
            "e2Nodes": [f"0x{nb:08x}" for nb in nodes.values()], "cells": [int(cell) for cell in cells],
            "plmn": {"mcc": "208", "mnc": "95"}})
        database = directory / "producer.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute("create table if not exists policies (policy_id text primary key, policy_type_id text, "
                               "policy_json text, status_json text, digest text, scope_key text, revision integer, "
                               "idempotency_key text, status_seq integer, producer_epoch integer, fenced integer, "
                               "updated_at text, not_before_ns integer, expires_at_ns integer)")
        return write("profile.json", {
            "schema": "oran-aic-phase-b-gui-profile/1.0.0", "profileId": "emulated-ran",
            "label": "Seeded hardware-free RAN", "capabilityManifestPath": str(capability),
            "integrationValuesPath": str(integration), "runsRoot": str(directory / "runs"),
            "metricSelection": ["RRU.PrbDl", "DRB.UEThpDl"],
            "liveConsole": {"assuranceBindingPath": str(binding), "producerDatabasePath": str(database),
                "r1StateDir": str(directory / "r1"), "evidenceDir": str(directory / "evidence"),
                "actionProducer": {"apiRoot": r1_root, "secretRefs": refs,
                    "policyTypes": {"AIC_UeDlPrbCap_1.0.0": {"actionId": "ue-dl-prb-cap", "adapter": "r1-cap"},
                                    "AIC_SchedulerPriority_1.0.0": {"actionId": "scheduler-priority", "adapter": "r1-priority"},
                                    **{action.policy_type_id: {"actionId": action.action_id,
                                         "adapter": action.adapter}
                                       for action in SUPPLEMENTARY_ACTIONS.values()
                                       if action.axis in {"dlMcsBounds", "txAttenuationDb", "slicePrbQuota"}}}}}})


@dataclass
class HardwareFreeAgentRuntime:
    """A real joint runtime plus the injection ports for another sitting root."""
    profile_path: Path
    read_new_lines: EmulatedKpmStream
    policy_port: Any
    adapter_overrides: dict[str, EmulatedActuationAdapter]
    ports: EmulatedClock
    counter_sample_loaders: dict[str, Any]
    kpi_observer: EmulatedKpiObserver
    ran: EmulatedRan
    joint: Any
    runtime: Any

    def sitting_kwargs(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in (
            "read_new_lines", "policy_port", "adapter_overrides", "ports", "counter_sample_loaders")}


def _counter_loaders(joint: Any, ran: EmulatedRan, clock: EmulatedClock,
                     adapters: Mapping[str, EmulatedActuationAdapter],
                     amf_of: Mapping[str, Any] = None) -> dict[str, Any]:
    amf_of = {} if amf_of is None else amf_of  # shared, like the stream's: re-registration is live
    from assurance.collector.samples import ClockHealth, RawSample
    from assurance.core.provenance import Provenance, TypedQuantity
    from assurance.live.objective_runtime import bundle_geometry
    loaders = {}
    for geometry in bundle_geometry(joint.bundle).counters:
        if geometry.deployment_counter_name == "UE.ServingCell":
            continue
        ue = str(geometry.scope.get("ueId") or geometry.scope.get("controlledUeId"))
        sequence = [0]

        def load(g=geometry, ue=ue, sequence=sequence):
            name = g.deployment_counter_name
            scope = {"amf_ue_ngap_id": str(amf_of.get(ue, ue))}  # the stream names the id, not the role
            cell = str(g.scope.get("cellId", "")).rsplit("-", 1)[-1]
            snssai = str(g.scope.get("sNssai", ""))
            sst = str(g.scope.get("sst") or snssai.split("-", 1)[0])
            if name == "RAN.Cell.DlMcsBounds":
                encoded = ran.axis_value(f"dlMcsBounds@{cell}")
                value, unit = ran.mcs_bounds[cell][1], "MCS-index"
                scope = {"nrCellDu": cell, "cellId": cell, "appliedValue": encoded}
            elif name == "RAN.Cell.TxAttenuationDb":
                value, unit = ran.tx_attenuation_db[cell], "dB"
                scope = {"nrCellDu": cell, "cellId": cell,
                         "appliedValue": ran.axis_value(f"txAttenuationDb@{cell}")}
            elif name == "L1M.SS-RSRP":
                # Effect observation only, never an attenuation configuration readback.
                value, unit = -80.0 - ran.tx_attenuation_db[cell], "dBm"
                scope = {"nrCellDu": cell, "cellId": cell}
            elif name == "RAN.SlicePrbQuotaMin":
                value, unit = ran.slice_quotas[sst][1], "percent"
                scope = {"sst": sst, "sNssai": snssai or sst,
                         "appliedValue": ran.axis_value(f"slicePrbQuota@{sst}")}
            elif name == "RAN.UE.DlPrbCap":
                value, unit = float(adapters[f"r1-cap@{ue}"].snapshot()[f"dlPrbCap@{ue}"]), "PRB"
            elif name == "RAN.UE.PfWeight":
                value, unit = float(adapters[f"r1-pf@{ue}"].snapshot()[f"pfWeight@{ue}"]), "ratio"
            elif name == "DRB.UEThpDl":
                value, unit = ran.dl_goodput()[ue] * 1000, "kbit/s"
            else:
                return ()
            seq = sequence[0]
            sequence[0] += 1
            sample_id = f"emulator:{g.counter_id}:{seq}"
            trace = json.dumps([clock.now(), scope, name, value])
            return (RawSample(sample_id=sample_id, counter_id=name,
                value=TypedQuantity(value, unit, Provenance.MEASURED, sample_id),
                scope_snapshot=scope, observed_at=clock.now(),
                cadence_ms=g.cadence_ms, clock_health=ClockHealth.SYNCHRONISED,
                trace_hash=hashlib.sha256(trace.encode()).hexdigest(), sequence=seq),)
        loaders[geometry.counter_id] = load
    return loaders


def build_hardware_free_agent_runtime(
    *, intents_spec: Sequence[IntentSpec], axes_spec: Sequence[Any], ran: EmulatedRan,
    budget_trials: int, tmp_dir: Any, epoch: int = 272,
    case_id: str = "case/hardware-free-agent",
) -> HardwareFreeAgentRuntime:
    """Compose existing typed joint specs over emulator ports, with no transport.

    ``axes_spec`` contains UE steering/cap/PF, cell MCS/attenuation and slice quota specs.
    PF specs use the existing contract's ratios (1.0 means physical weight 8).
    The result also exposes ``sitting_kwargs()`` for build_agent_sitting.
    """
    from assurance.live.joint_runtime import (
        SteeringParticipantSpec, SupplementaryParticipantSpec, build_joint_live_runtime,
    )
    from assurance.live.pin_to_cell_driver import KpmUeAttributionReader
    from tools.liveconsole import load_live_deployment
    from tools.liveconsole.build import live_topology

    profile = HermeticDeployment.write(tmp_dir, ues=ran.ues, cells=ran.cells, epoch=epoch)
    deployment = load_live_deployment(profile)
    steering = tuple(a for a in axes_spec if isinstance(a, SteeringAxisSpec))
    caps = tuple(a for a in axes_spec if isinstance(a, CapAxisSpec))
    priority = tuple(a for a in axes_spec if isinstance(a, PriorityAxisSpec))
    mcs = tuple(a for a in axes_spec if isinstance(a, McsBoundsAxisSpec))
    attenuation = tuple(a for a in axes_spec if isinstance(a, TxAttenuationAxisSpec))
    quota = tuple(a for a in axes_spec if isinstance(a, SlicePrbQuotaAxisSpec))
    joint = compose_joint(intents=intents_spec, steering_axes=steering, cap_axes=caps,
                          mcs_axes=mcs, attenuation_axes=attenuation, slice_quota_axes=quota,
                          priority_axes=priority, budget_trials=budget_trials,
                          deployment_binding=deployment.binding.r1.deployment)
    clock = EmulatedClock(ran)
    stream = EmulatedKpmStream(clock, ran, epoch)
    reader = KpmUeAttributionReader(read_new_lines=stream,
                                   topology=live_topology(deployment.binding, deployment.capability))
    labels = {str(amf): ue for ue, amf in stream.amf_of.items()}
    identities = {labels.get(str(item.amf_ue_ngap_id), str(item.amf_ue_ngap_id)): item
                  for item in reader.refresh()}
    adapters = {}
    participants, supplementary = [], []
    policy_port = EmulatedPolicyPort(POLICY_TYPE_ID)
    for spec in (*steering, *caps, *priority, *mcs, *attenuation, *quota):
        ratio = isinstance(spec, PriorityAxisSpec)
        adapter = EmulatedActuationAdapter(ran, spec.axis, baseline=spec.baseline, pf_ratio=ratio)
        adapters[adapter.name] = adapter
        if isinstance(spec, SteeringAxisSpec):
            participants.append(SteeringParticipantSpec(
                ue_id=spec.ue_id, identity=identities[spec.ue_id], policy_port=policy_port,
                policy_builder_factory=lambda *_: (lambda command: {}),
                adapter_key=adapter.name, adapter_override=adapter))
        else:
            supplementary.append(SupplementaryParticipantSpec(
                action_id=spec.action_id, axis=spec.axis, adapter_key=adapter.name, adapter=adapter))
    loaders = _counter_loaders(joint, ran, clock, adapters, stream.amf_of)
    runtime = build_joint_live_runtime(
        joint=joint, binding=deployment.binding, steering=participants, supplementary=supplementary,
        reader=reader, now=clock.now, monotonic_ms=clock.monotonic_ms, sleep_ms=clock.sleep_ms,
        case_id=case_id, counter_sample_loaders=loaders, arrival=clock.now)
    return HardwareFreeAgentRuntime(profile, stream, policy_port, adapters, clock, loaders,
                                    EmulatedKpiObserver(ran), ran, joint, runtime)
