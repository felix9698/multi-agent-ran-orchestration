"""Delivery-safe configuration for the packaged live-O1 coordinator.

The legacy top-level :mod:`config` module describes a particular OAI/USRP lab
and therefore is not an input to a portable upper artifact.  This module builds
the small duck-typed configuration the preserved Coordinator needs from the
two authority-bound documents already admitted by the release gate: the frozen
RAN capability manifest and deployment vector.

It intentionally contains no host address, SSH identity, device name, or local
filesystem path.  The injected rApp collector/executor ports do not consume
those legacy fields; compatibility properties return empty strings only because
the preserved constructor prepares (but never uses) a legacy collector map.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping


class ReleaseConfigError(ValueError):
    """Frozen capability/vector inputs cannot form one logical topology."""


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _cell_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ReleaseUeConfig:
    id: str
    initial_serving_gnb: str

    # The legacy Coordinator reads these while preparing the legacy collector
    # argument even when the release has injected its own collector.  Empty
    # compatibility properties cannot identify or contact a machine and are not
    # serialized into the release configuration.
    @property
    def hostname(self) -> str:
        return ""

    @property
    def ip(self) -> str:
        return ""

    @property
    def ssh_user(self) -> str:
        return ""


@dataclass(frozen=True)
class ReleaseGnbConfig:
    id: str
    # No PRB control is advertised by this release's capability.  A unit-sized
    # logical profile makes every legacy PRB proposal a deterministic no-op.
    num_prb: int = 1


@dataclass(frozen=True)
class ReleaseActionSpaceConfig:
    allowed_control_axes: tuple[str, ...]
    power_offset_min_db: float = 0.0
    power_offset_max_db: float = 0.0
    power_offset_neutral: float = 0.0
    prb_cap_min: int = 1
    prb_cap_max: int = 1
    prb_uncapped: int = 0
    sched_priority_min: float = 1.0
    sched_priority_max: float = 1.0
    sched_priority_neutral: float = 1.0
    mcs_offset_min: float = 0.0
    mcs_offset_max: float = 0.0
    mcs_offset_neutral: float = 0.0
    base_mcs_cap: int = 0
    ue_prb_cap_min: int = 1
    ue_prb_cap_max: int = 1
    ue_sched_priority_min: float = 1.0
    ue_sched_priority_max: float = 1.0

    AXES = ("power_offset", "prb", "sched_priority", "mcs_offset")
    PER_UE_AXES = ("prb", "sched_priority")

    def bounds(self, axis: str, per_ue: bool = False) -> tuple[float, float]:
        if per_ue:
            return {
                "prb": (self.ue_prb_cap_min, self.ue_prb_cap_max),
                "sched_priority": (
                    self.ue_sched_priority_min, self.ue_sched_priority_max),
            }[axis]
        return {
            "power_offset": (
                self.power_offset_min_db, self.power_offset_max_db),
            "prb": (self.prb_cap_min, self.prb_cap_max),
            "sched_priority": (
                self.sched_priority_min, self.sched_priority_max),
            "mcs_offset": (self.mcs_offset_min, self.mcs_offset_max),
        }[axis]

    def neutral(self, axis: str) -> float | int:
        return {
            "power_offset": self.power_offset_neutral,
            "prb": self.prb_uncapped,
            "sched_priority": self.sched_priority_neutral,
            "mcs_offset": self.mcs_offset_neutral,
        }[axis]

    def clip(self, axis: str, value: float, per_ue: bool = False) -> float | int:
        if axis == "prb":
            candidate = int(round(float(value)))
            if candidate <= 0:
                return self.prb_uncapped
            low, high = self.bounds(axis, per_ue)
            return int(max(low, min(high, candidate)))
        low, high = self.bounds(axis, per_ue)
        return max(low, min(high, float(value)))


@dataclass(frozen=True)
class ReleaseNetworkConfig:
    gnbs: dict[str, ReleaseGnbConfig]
    ues: dict[str, ReleaseUeConfig]
    action_space: ReleaseActionSpaceConfig


@dataclass
class ReleaseCalibrationConfig:
    """Normalized safety costs derived from frozen duration limits."""

    tau_trial: float
    hard_failure_cap_s: float
    negotiation_duration_cap_s: float
    policy_call_timeout_s: float
    episode_budget_s: float
    model_call_timeout_s: float
    commit_freshness_s: float
    max_fsm_steps: int = 256
    kpi_loss_cap_mbps: float = 1.0
    hard_failure_extra_penalty_mbps_s: float = 0.0
    action_space_profile: str = "oran-serving-cell-only"
    initial_n_max: int | None = None
    cold_start_policy: str = "frozen-duration-derived"
    cold_start_policy_reason: str = (
        "normalized unit loss; durations come from the admitted capability/vector")

    @property
    def c_worst(self) -> float:
        return self.c_hard_ub()

    @property
    def r_success(self) -> float:
        return self.kpi_loss_cap_mbps * self.tau_trial

    @property
    def c_nego(self) -> float:
        return self.c_nego_ub()

    @property
    def c_episode(self) -> float:
        return self.kpi_loss_cap_mbps * self.episode_budget_s

    def c_hard_ub(self) -> float:
        return (self.kpi_loss_cap_mbps * self.hard_failure_cap_s
                + self.hard_failure_extra_penalty_mbps_s)

    def c_trial_ub(self, tau_trial: float | None = None) -> float:
        tau = self.tau_trial if tau_trial is None else float(tau_trial)
        return self.kpi_loss_cap_mbps * max(0.0, tau) + self.c_hard_ub()

    def c_nego_ub(self) -> float:
        return self.kpi_loss_cap_mbps * self.negotiation_duration_cap_s

    def compute_initial_theta(self) -> float:
        denominator = self.r_success + self.c_worst
        if denominator <= 0:
            raise ReleaseConfigError("frozen timing produced no calibration scale")
        return max(0.0, min(1.0, (self.c_worst - self.c_nego) / denominator))

    def compute_initial_n_max(self, tau_trial: float | None = None) -> int:
        if self.initial_n_max is not None:
            return max(0, int(self.initial_n_max))
        trial = self.c_trial_ub(tau_trial)
        negotiation = self.c_nego_ub()
        if self.c_episode < trial or trial + negotiation <= 0:
            return 0
        return max(0, int(math.floor(
            (self.c_episode - trial) / (trial + negotiation))))

    def cold_start_source(self, tau_trial: float | None = None) -> dict[str, Any]:
        return {
            "policy": self.cold_start_policy,
            "reason": self.cold_start_policy_reason,
            "initial_theta": self.compute_initial_theta(),
            "initial_n_max": self.compute_initial_n_max(tau_trial),
            "c_trial_ub": self.c_trial_ub(tau_trial),
            "c_nego_ub": self.c_nego_ub(),
            "c_episode": self.c_episode,
        }

    def caps_audit(self, tau_trial: float | None = None) -> dict[str, Any]:
        tau = self.tau_trial if tau_trial is None else float(tau_trial)
        body: dict[str, Any] = {
            "kpi_loss_cap_mbps": self.kpi_loss_cap_mbps,
            "hard_failure_cap_s": self.hard_failure_cap_s,
            "negotiation_duration_cap_s": self.negotiation_duration_cap_s,
            "hard_failure_extra_penalty_mbps_s":
                self.hard_failure_extra_penalty_mbps_s,
            "tau_trial": tau,
            "action_space_profile": self.action_space_profile,
            "c_trial_ub": self.c_trial_ub(tau),
            "c_hard_ub": self.c_hard_ub(),
            "c_nego_ub": self.c_nego_ub(),
            "derivation": "normalized_loss * admitted_duration",
        }
        body["provenance_hash"] = "caps-" + hashlib.sha1(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode(
                "utf-8")).hexdigest()[:12]
        return body


@dataclass(frozen=True)
class ReleaseConfig:
    network: ReleaseNetworkConfig
    calibration: ReleaseCalibrationConfig
    simulation_mode: bool = True


def _seconds(value: Any, pointer: str) -> float:
    if isinstance(value, bool):
        raise ReleaseConfigError("%s must be a positive millisecond count" % pointer)
    try:
        seconds = float(value) / 1000.0
    except (TypeError, ValueError) as exc:
        raise ReleaseConfigError(
            "%s must be a positive millisecond count" % pointer) from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ReleaseConfigError("%s must be a positive millisecond count" % pointer)
    return seconds


def build_release_config(*, capability_manifest: Mapping[str, Any],
                         vector: Mapping[str, Any]) -> ReleaseConfig:
    """Build the Coordinator's portable logical configuration, fail closed."""
    capability = _copy(dict(capability_manifest or {}))
    deployment = _copy(dict(vector or {}))
    cap_topology = capability.get("topology", {})
    vector_topology = deployment.get("topology", {})
    cap_cells = cap_topology.get("cells", [])
    mappings = vector_topology.get("cellMappings", [])
    if not isinstance(cap_cells, list) or not cap_cells:
        raise ReleaseConfigError("capability topology declares no cells")
    if not isinstance(mappings, list) or len(mappings) != len(cap_cells):
        raise ReleaseConfigError(
            "deployment cell mappings must be bijective with capability cells")
    capability_keys = {_cell_key(item.get("cellId")) for item in cap_cells}
    mapping_keys = [_cell_key(item.get("cellId")) for item in mappings]
    if len(set(mapping_keys)) != len(mapping_keys) \
            or set(mapping_keys) != capability_keys:
        raise ReleaseConfigError(
            "deployment cell mappings differ from the frozen capability topology")
    if str(vector_topology.get("nearRtRicId")) != str(
            capability.get("nearRtRicId")):
        raise ReleaseConfigError(
            "deployment nearRtRicId differs from the frozen capability")

    gnb_by_cell: dict[str, str] = {}
    gnbs: dict[str, ReleaseGnbConfig] = {}
    for index, mapping in enumerate(mappings, 1):
        identifier = "gnb%d" % index
        key = _cell_key(mapping["cellId"])
        gnb_by_cell[key] = identifier
        gnbs[identifier] = ReleaseGnbConfig(id=identifier)
    serving_key = _cell_key(vector_topology.get("servingCell", {}).get("cellId"))
    serving_gnb = gnb_by_cell.get(serving_key)
    if serving_gnb is None:
        raise ReleaseConfigError(
            "deployment serving cell is absent from the cell mapping")

    controls = tuple(sorted(str(item) for item in capability.get(
        "controlAxes", []) if isinstance(item, str)))
    if "serving_cell" not in controls:
        raise ReleaseConfigError(
            "the frozen capability does not advertise serving_cell control")
    action_space = ReleaseActionSpaceConfig(allowed_control_axes=controls)
    network = ReleaseNetworkConfig(
        gnbs=gnbs,
        ues={"ue-live": ReleaseUeConfig(
            id="ue-live", initial_serving_gnb=serving_gnb)},
        action_space=action_space)

    timing = capability.get("timing", {})
    timeouts = deployment.get("timeouts", {})
    step = _seconds(timeouts.get("defaultStepMs"),
                    "/timeouts/defaultStepMs")
    recovery = _seconds(timing.get("recoveryWindowMs"),
                        "/capability/timing/recoveryWindowMs")
    deadline = _seconds(timing.get("maxActionDeadlineMs"),
                        "/capability/timing/maxActionDeadlineMs")
    freshness = _seconds(timing.get("assuranceFreshnessLimitMs"),
                         "/capability/timing/assuranceFreshnessLimitMs")
    calibration = ReleaseCalibrationConfig(
        tau_trial=step,
        hard_failure_cap_s=recovery,
        negotiation_duration_cap_s=min(step, recovery),
        policy_call_timeout_s=step,
        model_call_timeout_s=step,
        episode_budget_s=deadline,
        commit_freshness_s=freshness)
    # Force all derived values now; invalid arithmetic refuses construction,
    # rather than surfacing halfway through the Coordinator's first episode.
    calibration.compute_initial_theta()
    calibration.compute_initial_n_max()
    calibration.caps_audit()
    return ReleaseConfig(network=network, calibration=calibration)


__all__ = [
    "ReleaseActionSpaceConfig", "ReleaseCalibrationConfig", "ReleaseConfig",
    "ReleaseConfigError", "ReleaseGnbConfig", "ReleaseNetworkConfig",
    "ReleaseUeConfig", "build_release_config",
]
