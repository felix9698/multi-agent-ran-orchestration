#!/usr/bin/env python3
"""
Experiment topology (Batch G, P0-18): the SINGLE DYNAMIC source of truth for the
UE<->BS attachment, whatever the CURRENT UE count is.

UE COUNT IS VARIABLE. The architecture is N-UE: every consumer - prompt
enumeration, parser/scope validation, collector, executor addressing, dashboard,
export - reads the UE set from a CONFIGURED topology object, NEVER a hardcoded
list or a fixed count (CLAUDE.md: "Avoid hardcoded UE lists"). This module builds
that object from a `NetworkConfig` so adding/removing a UE in config propagates
everywhere with no code change.

CURRENT TESTBED FACT (2026-07): two physically-provisioned UEs - UE1 and UE2 -
both attach to gNB1 and genuinely contend for its cell resource (an intra-cell
fairness scenario on a 2-UE SHARED cell). A third UE (UE3) exists only LOGICALLY
in config with no provisioned host/IMSI; it is a planned EXPANSION and is
fail-closed out of every real path until provisioned (config.is_physically_
provisioned). `provisioned_emulation_topology()` is the dynamic default and
tracks exactly this real set; when UE3+ is provisioned it joins automatically.
`three_ue_shared_topology()` remains available as an explicit OPTIONAL scenario
(e.g. offline what-if), NOT a description of present hardware. See
docs/ue_count_variability.md; the §V-B "3-UE topology" derivation is INVALID
(§I-IV place no UE-count/placement requirement - docs/paper_alignment_sec1_4.md).

Design contract:
  * A topology is (ue_serving: ue_id -> gnb_id, ue_rnti: ue_id -> rnti). Every
    derived quantity (bs set, shared cells, contending UEs) is COMPUTED, never
    stored redundantly, so there is no second place to keep in sync.
  * "Shared cell" = a BS serving >= 2 UEs. The PRIMARY shared cell is the first
    one in BS order; its UEs are the contending UEs whose fairness the
    scheduling-priority / PRB axes partition.
  * frozen dataclass: a topology is immutable once built (an experiment block
    replays one topology to every method, P0-20).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Dict, List, Mapping, Optional, Tuple


# Deterministic RNTIs for the EMULATED / OFFLINE topologies ONLY. Real OTA RNTIs
# are assigned by the gNB at attach and MUST be resolved live
# (executor._resolve_ue_rnti); they are NEVER guessed here. A live/config-derived
# topology therefore carries NO RNTIs (from_network_config leaves them empty).
_EMULATION_RNTI_BASE = 0x4600
# Canonical ids keep their historical literal RNTIs so emulated fixtures stay
# byte-stable; ue5, ue6, ... are DERIVED (emulation_rnti) - never a fixed table
# that would cap the emulated UE count.
_EMULATION_RNTI = {"ue1": 0x4601, "ue2": 0x4602, "ue3": 0x4603, "ue4": 0x4604}


def emulation_rnti(ue_id: str) -> int:
    """Deterministic EMULATION-ONLY RNTI for a UE id (NEVER an OTA value).

    Derived from the trailing integer in the id: ue1->0x4601, ue2->0x4602, ...,
    ueN->0x4600+N. This scales to ANY number of emulated UEs (ue4, ue5, ...) with
    NO fixed table and keeps RNTIs distinct within a cell. The canonical ue1..ue4
    keep their historical literals. A UE id without a positive numeric suffix
    falls back to a stable, collision-avoiding offset (still emulation-only)."""
    if ue_id in _EMULATION_RNTI:
        return _EMULATION_RNTI[ue_id]
    m = re.search(r"(\d+)$", str(ue_id))
    if m and int(m.group(1)) > 0:
        return _EMULATION_RNTI_BASE + int(m.group(1))
    # non-numeric id (defensive; ids in this project are always ueN): a stable
    # per-id offset above the numeric range, deterministic across processes.
    return _EMULATION_RNTI_BASE + 0x100 + (sum(ord(c) for c in str(ue_id)) % 0xF00)


@dataclass(frozen=True)
class ExperimentTopology:
    """An immutable UE<->BS attachment map plus per-UE RNTIs.

    All list/tuple accessors preserve INSERTION ORDER of ``ue_serving`` so the
    enumerated UE set is deterministic across processes (dict preserves order).
    """
    ue_serving: Mapping[str, str]
    ue_rnti: Mapping[str, int]
    label: str = "custom"
    # The configured BS id set to PRESERVE (config order), when derived from a
    # NetworkConfig. Empty => bs_ids() is derived from the serving map. Kept so a
    # config-derived topology never drops/renames a configured gNB id even if it
    # currently serves no UE.
    configured_bs_ids: Tuple[str, ...] = ()

    def __post_init__(self):
        # DEEP-IMMUTABILITY (review blocker): copy the inputs into private dicts
        # and expose them ONLY through read-only MappingProxyType, so neither
        # `t.ue_serving['ue1']='x'` (item assignment) nor aliasing the caller's
        # original dict can mutate a built topology.
        object.__setattr__(self, "ue_serving",
                           MappingProxyType(dict(self.ue_serving)))
        object.__setattr__(self, "ue_rnti",
                           MappingProxyType(dict(self.ue_rnti)))
        object.__setattr__(self, "configured_bs_ids",
                           tuple(self.configured_bs_ids))
        # fail-closed: every UE must have a serving gNB and (for the offline /
        # emulated fakes) a distinct RNTI within its cell.
        for ue, gnb in self.ue_serving.items():
            if not isinstance(ue, str) or not isinstance(gnb, str) or not gnb:
                raise ValueError(f"invalid UE->gNB mapping {ue!r}->{gnb!r}")
        # RNTI uniqueness WITHIN a cell (two UEs on one cell must be
        # distinguishable to the scheduler for intra-cell fairness to be real).
        by_cell: Dict[str, set] = {}
        for ue, gnb in self.ue_serving.items():
            r = self.ue_rnti.get(ue)
            if r is None:
                continue
            cell = by_cell.setdefault(gnb, set())
            if r in cell:
                raise ValueError(
                    f"duplicate RNTI {r:#06x} within cell {gnb!r} "
                    f"(UEs sharing a cell must have distinct RNTIs)")
            cell.add(int(r))
        # When a configured BS set is preserved, every serving gNB must be a
        # member (a topology cannot reference a gNB that is not configured).
        if self.configured_bs_ids:
            bs_set = set(self.configured_bs_ids)
            for ue, gnb in self.ue_serving.items():
                if gnb not in bs_set:
                    raise ValueError(
                        f"UE {ue!r} serves gNB {gnb!r} which is not in the "
                        f"configured BS set {list(self.configured_bs_ids)}")

    # -- enumeration --------------------------------------------------------- #

    def ue_ids(self) -> Tuple[str, ...]:
        """Configured UE set, in configuration order (never a hardcoded list)."""
        return tuple(self.ue_serving.keys())

    def bs_ids(self) -> Tuple[str, ...]:
        """The configured BS ids when preserved (config order), else the distinct
        serving gNBs in first-seen order. Preserving the configured set means a
        gNB that currently serves no UE is still enumerated."""
        if self.configured_bs_ids:
            return tuple(self.configured_bs_ids)
        seen: List[str] = []
        for gnb in self.ue_serving.values():
            if gnb not in seen:
                seen.append(gnb)
        return tuple(seen)

    def serving_gnb(self, ue_id: str) -> Optional[str]:
        return self.ue_serving.get(ue_id)

    def rnti(self, ue_id: str) -> Optional[int]:
        return self.ue_rnti.get(ue_id)

    def ues_on(self, gnb_id: str) -> Tuple[str, ...]:
        return tuple(ue for ue, g in self.ue_serving.items() if g == gnb_id)

    def is_shared(self, gnb_id: str) -> bool:
        return len(self.ues_on(gnb_id)) >= 2

    def shared_cells(self) -> Tuple[str, ...]:
        return tuple(g for g in self.bs_ids() if self.is_shared(g))

    def primary_shared_cell(self) -> Optional[str]:
        """The first BS serving >= 2 UEs (the paper's contended primary cell)."""
        shared = self.shared_cells()
        return shared[0] if shared else None

    def contending_ues(self) -> Tuple[str, ...]:
        """UEs that genuinely contend for the primary shared cell's resource."""
        cell = self.primary_shared_cell()
        return self.ues_on(cell) if cell else ()

    def n_ues(self) -> int:
        return len(self.ue_serving)

    def n_bs(self) -> int:
        return len(self.bs_ids())

    # -- serialization (provenance / raw export) ----------------------------- #

    def to_dict(self) -> Dict:
        return {
            "label": self.label,
            "ue_serving": dict(self.ue_serving),
            "ue_rnti": {ue: int(r) for ue, r in self.ue_rnti.items()},
            "bs_ids": list(self.bs_ids()),
            "shared_cells": list(self.shared_cells()),
            "primary_shared_cell": self.primary_shared_cell(),
            "contending_ues": list(self.contending_ues()),
            "n_ues": self.n_ues(),
            "n_bs": self.n_bs(),
        }


def three_ue_shared_topology(
        serving: str = "gnb1", neighbor: str = "gnb2") -> ExperimentTopology:
    """An OPTIONAL 3-UE logical topology: UE1+UE2 -> `serving` (shared,
    contending), UE3 -> `neighbor`. Built dynamically so all consumers enumerate
    {ue1,ue2,ue3}.

    THIS IS A SCENARIO OPTION, NOT THE CURRENT TESTBED. The live testbed has two
    provisioned UEs (see provisioned_emulation_topology / docs/ue_count_
    variability.md). Use this only to explore a future 3-UE layout offline, e.g.
    once UE3 is provisioned. The §V-B "3-UE" derivation is invalid basis
    (docs/paper_alignment_sec1_4.md).

    EMULATION/OFFLINE ONLY for the RNTIs: this constructor stamps deterministic
    emulation_rnti values for the in-memory SimTelnetGNB. A LIVE run must derive
    the logical topology from config (from_network_config) and resolve RNTIs from
    the real gNB - never these guessed values."""
    return ExperimentTopology(
        ue_serving={"ue1": serving, "ue2": serving, "ue3": neighbor},
        ue_rnti={ue: emulation_rnti(ue) for ue in ("ue1", "ue2", "ue3")},
        label="3ue_shared_cell(emulated_rnti)")


def two_ue_topology(serving: str = "gnb1",
                    neighbor: str = "gnb2") -> ExperimentTopology:
    """Legacy 2-UE topology (one UE per cell, no shared-cell contention). Kept
    for the Batch A-F emulation defaults and single-cell profiles. RNTIs are
    EMULATION-ONLY (see three_ue_shared_topology).

    NOTE: this is the ONE-UE-PER-CELL layout (UE1->gNB1, UE2->gNB2). It is NOT
    the current testbed's 2-UE SHARED cell (UE1+UE2 both on gNB1); for that real
    provisioned layout use provisioned_emulation_topology()."""
    return ExperimentTopology(
        ue_serving={"ue1": serving, "ue2": neighbor},
        ue_rnti={ue: emulation_rnti(ue) for ue in ("ue1", "ue2")},
        label="2ue(emulated_rnti)")


def provisioned_emulation_topology(network=None,
                                   label: str = "provisioned(emulated_rnti)"
                                   ) -> ExperimentTopology:
    """The DEFAULT emulated/offline topology: the CURRENTLY PROVISIONED real UE
    set, derived from configuration - never a hardcoded count or a fixed 3-UE
    assumption.

    Each PHYSICALLY-PROVISIONED UE (config.is_physically_provisioned) keeps its
    configured `initial_serving_gnb` and receives a deterministic emulation RNTI.
    On the current testbed this yields UE1+UE2 both on gNB1 - a 2-UE SHARED cell
    (they genuinely contend for gNB1's resource). When a 3rd (or 4th...) UE is
    provisioned - its host/IMSI supplied via config or the UE<N>_* env - it
    AUTOMATICALLY joins here with no code change, so the emulated default always
    tracks the real testbed instead of asserting a fixed 3-UE topology.

    Unprovisioned logical UEs (e.g. UE3 today) are excluded - the same
    fail-closed intent as Config.validate_live_topology, so the default emulation
    mirrors what actually exists. If NOTHING is provisioned (bare/headless env),
    falls back to two_ue_topology() so import/standalone use never hard-fails.

    EMULATION/OFFLINE ONLY for the RNTIs (see emulation_rnti)."""
    if network is None:
        from config import get_config
        network = get_config().network
    configured_bs = tuple(getattr(network, "gnbs", {}).keys())
    bs_set = set(configured_bs)
    ue_serving: Dict[str, str] = {}
    ue_rnti: Dict[str, int] = {}
    for ue_id, ue in getattr(network, "ues", {}).items():
        is_prov = getattr(ue, "is_physically_provisioned", None)
        if callable(is_prov) and not is_prov():
            continue                       # fail-closed: only real UEs by default
        gnb = getattr(ue, "initial_serving_gnb", None)
        if not gnb or (bs_set and gnb not in bs_set):
            continue
        ue_serving[ue_id] = gnb
        ue_rnti[ue_id] = emulation_rnti(ue_id)
    if not ue_serving:
        return two_ue_topology()
    return ExperimentTopology(ue_serving=ue_serving, ue_rnti=ue_rnti,
                              configured_bs_ids=configured_bs, label=label)


def from_network_config(network, label: str = "config") -> ExperimentTopology:
    """Derive the LIVE / CONFIGURED logical topology from a `config.NetworkConfig`.

    UE->gNB comes from each UEConfig.initial_serving_gnb. Crucially it carries NO
    RNTIs: real OTA RNTIs are assigned by the gNB at attach and are resolved LIVE
    (executor._resolve_ue_rnti); guessing them here would be a fabricated
    hardware identity. Consumers that need an RNTI on a live run must resolve it
    from the gNB, not from the topology.

    The serving gNB of every UE is VALIDATED to be a configured gNB, and the
    configured BS ids are PRESERVED (config order) so bs_ids() reflects the real
    configured cells - not just those that happen to serve a UE."""
    configured_bs = tuple(network.gnbs.keys())
    bs_set = set(configured_bs)
    ue_serving: Dict[str, str] = {}
    for ue_id, ue in network.ues.items():
        gnb = getattr(ue, "initial_serving_gnb", None)
        if not gnb:
            raise ValueError(f"UE {ue_id!r} has no initial_serving_gnb")
        if gnb not in bs_set:
            raise ValueError(
                f"UE {ue_id!r} serves gNB {gnb!r} which is not a configured "
                f"gNB (configured: {list(configured_bs)})")
        ue_serving[ue_id] = gnb
    return ExperimentTopology(ue_serving=ue_serving, ue_rnti={},
                              configured_bs_ids=configured_bs, label=label)


def add_adversarial_ue(topology: ExperimentTopology, ue_id: str = "ue4",
                       gnb_id: Optional[str] = None) -> ExperimentTopology:
    """Return a NEW topology with a 4th UE added to the primary shared cell (or
    `gnb_id`). Used by the adversarial dynamic-4th-UE coverage: everything that
    enumerates the configured UE set must pick it up without a code change. Only
    stamps an emulation RNTI when the source topology already has emulation RNTIs
    (a live/config topology with no RNTIs stays RNTI-free - resolved live)."""
    gnb_id = gnb_id or topology.primary_shared_cell() or topology.bs_ids()[0]
    serving = dict(topology.ue_serving)
    rnti = dict(topology.ue_rnti)
    serving[ue_id] = gnb_id
    if rnti:   # only an emulation topology carries RNTIs; keep live ones empty
        rnti[ue_id] = emulation_rnti(ue_id)
    return ExperimentTopology(ue_serving=serving, ue_rnti=rnti,
                              label=topology.label + "+adv")
