#!/usr/bin/env python3
"""
Emulation mode (P6): the REAL decision pipeline over an emulated RAN.

Unlike experiments/synthetic.py (a harness validator with pre-injected
per-method success profiles), this module wires the real IntentCoordinator -
S0-S6 state machine, enforcement clipping, snapshot/rollback, polled S4
validation, round-based negotiation, adaptive calibration - to an in-memory
gNB + channel model, with the deterministic mock LLM backend
("mock:deterministic") driving decisions. Only the radio is simulated.

Components
  * SimTelnetGNB : parses the patched "ci" telnet command set
                   (rfatt / prbcap / sched_prio / mcs / get_single_rnti /
                   get_reestab_count) and answers in the PATCH'S EXACT output
                   format, so write-then-read-back verification (P7) works
                   against it unchanged. Fault injection: fail_axes (apply
                   failures), reestablish() (RNTI change, P8).
  * SimExecutor  : OAIExecutor subclass overriding ONLY the telnet transport
                   (_telnet_cmd) - clipping, mirror state, snapshot/restore
                   are the real production code paths.
  * ChannelModel : tput(ue) = clip(base + a*(env_s + off_s)
                                   - b*|env_n + off_n|
                                   + prb_term + sched_term + N(0, sigma)).
                   ENVIRONMENT gains are a separate input from executor
                   action offsets (resolves docs/experiment_plan.md OQ-EXP-1:
                   I1 constrains only the ACTION). fit() is a placeholder for
                   the Phase-2 testbed grid sweep.
  * SimCollector : collect_all() -> {ue: UEMetrics}, get_throughput_all(),
                   simulation_mode=False (the coordinator takes the REAL
                   hardware paths, not the simulation shortcuts).
  * build_emulated_coordinator(): assembles the above around a real
                   IntentCoordinator.

Determinism: all randomness flows from seeded random.Random instances
(ChannelModel + mock LLM). Do not start() the coordinator (the background
metrics thread would consume channel RNG draws nondeterministically).
"""

from __future__ import annotations

import logging
import math
import random
from typing import Dict, List, Optional, Tuple

from collectors.multi_ue_collector import UEMetrics
from executor.oai_executor import (
    OAIExecutor, BASE_ATT_DB, BASE_MCS_CAP, PRB_UNCAPPED, PRIO_NEUTRAL,
)

logger = logging.getLogger("Emulation")

# fixed emulation topology (mirrors the paper: UE1 constrained on BS1,
# BS2 the interferer/serving UE2)
EMU_UE_SERVING = {"ue1": "gnb1", "ue2": "gnb2"}
EMU_UE_RNTI = {"ue1": 0x4601, "ue2": 0x4602}


# --------------------------------------------------------------------------- #
# SimTelnetGNB                                                                 #
# --------------------------------------------------------------------------- #

class SimTelnetGNB:
    """In-memory gNB speaking the patched telnet "ci" command set.

    Response strings replicate oai_patches/d2_actionspace_runtime_knobs.patch
    printf formats exactly (see the patch: "DL PRB cap set to %d",
    "UE %04x PF weight set to %.3f", "DL MCS cap set to [%d..%d]", ...), so
    the real OAIExecutor parses them unmodified and P7's readback
    verification can diff mirror vs device.
    """

    def __init__(self, pci: int = 0, fail_axes: Optional[set] = None):
        self.pci = pci
        self.att_db = BASE_ATT_DB
        self.dl_prb_cap = PRB_UNCAPPED
        self.mcs_min = 0
        self.mcs_max = BASE_MCS_CAP
        # rnti -> {"pf_weight": float, "prb_cap": int}
        self.ues: Dict[int, Dict] = {}
        self.reestab_count = 0
        # inject apply failures per axis: subset of
        # {"rfatt", "prbcap", "sched_prio", "mcs"}
        self.fail_axes = set(fail_axes or ())
        # inject SILENT failures: the gNB acknowledges ("set to ...") but
        # does NOT apply - exactly what P7's read-back verification catches
        self.silent_fail_axes: set = set()
        self.commands: List[str] = []   # every command received (assertions)

    # -- topology / fault injection -------------------------------------- #

    def add_ue(self, rnti: int):
        self.ues[int(rnti)] = {"pf_weight": PRIO_NEUTRAL,
                               "prb_cap": PRB_UNCAPPED}

    def detach_ue(self, rnti: int):
        self.ues.pop(int(rnti), None)

    def reestablish(self, old_rnti: int, new_rnti: int):
        """RRC re-establishment: the UE returns under a NEW RNTI with a
        FRESH scheduler context (per-UE knobs reset to neutral) - exactly
        the state divergence the P8 guard must detect and repair."""
        self.ues.pop(int(old_rnti), None)
        self.ues[int(new_rnti)] = {"pf_weight": PRIO_NEUTRAL,
                                   "prb_cap": PRB_UNCAPPED}
        self.reestab_count += 1

    # -- command execution ------------------------------------------------ #

    def exec_cmd(self, cmd: str) -> str:
        """Execute one "ci ..." command, return the response text."""
        self.commands.append(cmd)
        parts = cmd.split()
        if len(parts) < 2 or parts[0] != "ci":
            return f"unknown command '{cmd}'"
        op, args = parts[1], parts[2:]
        handler = {
            "rfatt": self._cmd_rfatt,
            "prbcap": self._cmd_prbcap,
            "sched_prio": self._cmd_sched_prio,
            "mcs": self._cmd_mcs,
            "get_single_rnti": self._cmd_get_single_rnti,
            "get_reestab_count": self._cmd_get_reestab_count,
        }.get(op)
        if handler is None:
            return f"unknown ci subcommand '{op}'"
        return handler(args)

    def _cmd_rfatt(self, args: List[str]) -> str:
        if not args:
            return f"current TX attenuation {self.att_db:.1f} dB"
        if "rfatt" in self.fail_axes:
            return "error: simulated failure"
        if "rfatt" not in self.silent_fail_axes:
            self.att_db = float(args[0])
        return f"TX attenuation set to {float(args[0]):.1f} dB"

    def _cmd_prbcap(self, args: List[str]) -> str:
        if not args:
            if self.dl_prb_cap == PRB_UNCAPPED:
                lines = ["DL PRB cap 0 (uncapped)"]
            else:
                lines = [f"DL PRB cap {self.dl_prb_cap}"]
            # patch fidelity: per-UE rows only for UEs with a cap > 0
            lines += [f"UE {rnti:04x} DL PRB cap {st['prb_cap']}"
                      for rnti, st in sorted(self.ues.items())
                      if st["prb_cap"] > 0]
            return "\n".join(lines)
        if "prbcap" in self.fail_axes:
            return "error: simulated failure"
        n = int(args[0])
        tail = " (uncapped)" if n == PRB_UNCAPPED else ""
        silent = "prbcap" in self.silent_fail_axes
        if len(args) >= 2:                       # per-UE form (hex RNTI)
            rnti = int(args[1], 16)
            if rnti not in self.ues:
                return f"error: no UE with RNTI {rnti:04x}"
            if not silent:
                self.ues[rnti]["prb_cap"] = n
            return f"UE {rnti:04x} DL PRB cap set to {n}{tail}"
        if not silent:
            self.dl_prb_cap = n
        return f"DL PRB cap set to {n}{tail}"

    def _cmd_sched_prio(self, args: List[str]) -> str:
        if not args:
            return "\n".join(f"UE {rnti:04x} PF weight {st['pf_weight']:.3f}"
                             for rnti, st in sorted(self.ues.items())) \
                or "no UEs connected"
        if "sched_prio" in self.fail_axes:
            return "error: simulated failure"
        w = float(args[0])
        if len(args) >= 2:
            rnti = int(args[1], 16)
            if rnti not in self.ues:
                return f"error: no UE with RNTI {rnti:04x}"
        elif len(self.ues) == 1:
            rnti = next(iter(self.ues))
        else:
            return "error: 0 or multiple UEs connected; specify RNTI"
        if "sched_prio" not in self.silent_fail_axes:
            self.ues[rnti]["pf_weight"] = w
        return f"UE {rnti:04x} PF weight set to {w:.3f}"

    def _cmd_mcs(self, args: List[str]) -> str:
        if not args:
            return (f"DL MCS cap [{self.mcs_min}..{self.mcs_max}] "
                    f"UL MCS cap [0..{BASE_MCS_CAP}]")
        if "mcs" in self.fail_axes:
            return "error: simulated failure"
        if "mcs" not in self.silent_fail_axes:
            self.mcs_max = int(args[0])
            if len(args) >= 2:
                self.mcs_min = int(args[1])
        return f"DL MCS cap set to [{self.mcs_min}..{int(args[0])}]"

    def _cmd_get_single_rnti(self, args: List[str]) -> str:
        if len(self.ues) == 1:
            return f"single UE RNTI {next(iter(self.ues)):04x}"
        return f"error: {len(self.ues)} UEs connected"

    def _cmd_get_reestab_count(self, args: List[str]) -> str:
        return f"reestab count {self.reestab_count}"


# --------------------------------------------------------------------------- #
# SimExecutor                                                                  #
# --------------------------------------------------------------------------- #

class SimExecutor(OAIExecutor):
    """OAIExecutor whose telnet transport is routed to SimTelnetGNBs.

    ONLY _telnet_cmd is overridden: enforcement clamps, the mirror state,
    apply_axis dispatch, and diff-based snapshot/restore are the real
    production code, exercised end to end.
    """

    def __init__(self, sim_gnbs: Dict[str, SimTelnetGNB],
                 gnb_configs: Optional[Dict[str, Dict]] = None):
        super().__init__(gnb_configs=gnb_configs)
        self.sim_gnbs = dict(sim_gnbs)
        # host -> sim (the transport addresses gNBs by host)
        self._sim_by_host = {
            self.states[gid].host: sim for gid, sim in self.sim_gnbs.items()
            if gid in self.states
        }

    def _telnet_cmd(self, host: str, cmd: str,
                    timeout: float = 3.0) -> Tuple[bool, str]:
        sim = self._sim_by_host.get(host)
        if sim is None:
            return False, f"no emulated gNB at {host}"
        return True, sim.exec_cmd(cmd)


# --------------------------------------------------------------------------- #
# ChannelModel                                                                 #
# --------------------------------------------------------------------------- #

class ChannelModel:
    """Deterministic (seeded) per-UE throughput model.

        tput(ue) = clip( base
                         + A * (env[serving] + action_off[serving])
                         - B * |env[neighbor] + action_off[neighbor]|
                         + prb_term + sched_term
                         + N(0, sigma) )

    Environment gains (the phase script) are a SEPARATE input from the
    executor's action offsets: I1 constrains only the action (OQ-EXP-1).
    Coefficients start at a=0.6, b=0.3, sigma=0.4 with base tuned so the
    8 Mbps I2 line is crossed in both directions across the 5 phases
    (Nominal +3/0 -> 10.3, Degradation -2/+1 -> 7.0, Impairment -5/+2 ->
    4.9, Recovery 0/0 -> 8.5, Nominal +3/0 -> 10.3 for the constrained UE).
    """

    def __init__(self, seed: int = 12345, base_tput: float = 8.5,
                 a: float = 0.6, b: float = 0.3, sigma: float = 0.4,
                 tput_min: float = 0.0, tput_max: float = 30.0):
        self.rng = random.Random(int(seed))
        self.base_tput = base_tput
        self.a = a
        self.b = b
        self.sigma = sigma
        self.tput_min = tput_min
        self.tput_max = tput_max
        # environment gains per gNB (phase script); gnb1 = the paper's
        # serving/constrained cell, gnb2 = the neighbor/interferer
        self.env_gain: Dict[str, float] = {"gnb1": 0.0, "gnb2": 0.0}
        # Batch G (P0-19): additional EXOGENOUS environment axes carried by the
        # canonical trace - an external disturbance (dB, additive) and an offered
        # load (Mbps, a goodput ceiling). Neutral defaults (0.0 / None) preserve
        # the existing deterministic behaviour exactly.
        self.external_disturbance_db: float = 0.0
        self.offered_load_mbps: Optional[float] = None

    def set_environment(self, serving_gain: float, neighbor_gain: float,
                        external_disturbance_db: float = 0.0,
                        offered_load_mbps: Optional[float] = None):
        """Apply one experiment phase's environment (NOT executor actions).

        `external_disturbance_db` and `offered_load_mbps` are the additional
        environment axes replayed from the canonical trace (P0-19); they are
        consumed by throughput() so the trace is never ignored. Neutral values
        (0.0 / None) reproduce the legacy behaviour."""
        self.env_gain["gnb1"] = float(serving_gain)
        self.env_gain["gnb2"] = float(neighbor_gain)
        self.external_disturbance_db = float(external_disturbance_db or 0.0)
        self.offered_load_mbps = (None if offered_load_mbps is None
                                  else float(offered_load_mbps))

    def reseed(self, seed: int) -> None:
        """Restart the channel RNG deterministically (Batch E paired ablation)."""
        self.rng = random.Random(int(seed))

    def capture_state(self):
        """LOSSLESS snapshot of ALL mutable channel state (P0-20): the RNG state
        AND every mutable environment field (per-gNB env gains, external
        disturbance, offered load). Capturing only the RNG would leak the phase
        environment across paired methods/blocks, so all of it is snapshotted."""
        return {
            "rng": self.rng.getstate(),
            "env_gain": dict(self.env_gain),
            "external_disturbance_db": self.external_disturbance_db,
            "offered_load_mbps": self.offered_load_mbps,
        }

    def restore_state(self, state) -> None:
        """Restore ALL mutable channel state from capture_state (lossless).
        Accepts a legacy RNG-only state for backward compatibility."""
        if isinstance(state, dict):
            self.rng.setstate(state["rng"])
            self.env_gain = dict(state["env_gain"])
            self.external_disturbance_db = state["external_disturbance_db"]
            self.offered_load_mbps = state["offered_load_mbps"]
        else:                                    # legacy: RNG-only opaque state
            self.rng.setstate(state)

    def throughput(self, serving_gnb: str, neighbor_gnb: str,
                   serving_off: float, neighbor_off: float,
                   prb_cap: int = PRB_UNCAPPED,
                   pf_weight: float = PRIO_NEUTRAL) -> float:
        s = self.env_gain.get(serving_gnb, 0.0) + float(serving_off)
        n = self.env_gain.get(neighbor_gnb, 0.0) + float(neighbor_off)
        val = self.base_tput + self.a * s - self.b * abs(n)
        # PRB cap: linear penalty below the full 106-PRB carrier
        if prb_cap and prb_cap > 0:
            val += -0.04 * max(0, 106 - int(prb_cap))
        # PF weight: gentle log2 boost/starve around neutral 1.0
        val += 1.5 * math.log2(max(float(pf_weight), 0.05))
        # exogenous external disturbance (dB, additive) - a trace environment axis
        val += -0.3 * abs(self.external_disturbance_db)
        val += self.rng.gauss(0.0, self.sigma)
        val = max(self.tput_min, min(self.tput_max, val))
        # offered load is a GOODPUT CEILING: goodput can never exceed the load
        # the traffic source actually offers (a trace environment axis).
        if self.offered_load_mbps is not None:
            val = min(val, self.offered_load_mbps)
        return val

    def contended_throughputs(
            self, serving_gnb: str, neighbor_gnb: str,
            serving_off: float, neighbor_off: float,
            weights: Dict[str, float],
            prb_cap: int = PRB_UNCAPPED) -> Dict[str, float]:
        """Normalized shared-cell contention (P0-18).

        The UEs sharing one cell partition a SINGLE bounded cell capacity by
        proportional-fair (PF) share. The capacity is this cell's channel
        throughput at the PF-NEUTRAL weight (one fresh channel draw per call,
        deterministic at sigma=0), and each UE receives ``capacity * share`` with
        ``share_i = w_i / sum_j w_j``. Consequences that hold EXACTLY at
        sigma=0:
          * raising one UE's PF weight raises its own share/KPI and STRICTLY
            lowers every other contending UE's share/KPI;
          * the shares sum to 1, so the total cell allocation never exceeds
            capacity (bounded contention);
          * a UE on a DIFFERENT cell is not in ``weights`` and is unaffected
            (no cross-cell coupling here).
        Weights are floored at a small epsilon so a zero/negative weight cannot
        starve the split into a divide-by-zero."""
        capacity = self.throughput(
            serving_gnb, neighbor_gnb, serving_off, neighbor_off,
            prb_cap=prb_cap, pf_weight=PRIO_NEUTRAL)
        eps = 0.05
        pos = {ue: max(float(w), eps) for ue, w in weights.items()}
        total = sum(pos.values()) or 1.0
        return {ue: capacity * (w / total) for ue, w in pos.items()}

    def fit(self, grid_jsonl_path: str):
        """Placeholder: fit (a, b, sigma, base) from a Phase-2 testbed grid
        sweep (JSONL of {serving_off, neighbor_off, prb_cap, pf_weight,
        tput} rows). Deliberately separate so emulation constants and the
        eventual measured fit share this one entry point."""
        raise NotImplementedError(
            "ChannelModel.fit is a Phase-2 testbed task (grid sweep JSONL)")


# --------------------------------------------------------------------------- #
# SimCollector                                                                 #
# --------------------------------------------------------------------------- #

class SimCollector:
    """Collector over the emulated RAN: real UEMetrics, no hardware.

    simulation_mode=False ON PURPOSE: the coordinator must take its real
    hardware code paths (polled S4 window, trial application, restore), not
    the simulation shortcuts.
    """

    simulation_mode = False
    # Marks this collector as the EMULATED RAN (no real hardware), so the
    # coordinator's live-startup fail-closed UE-provisioning check (P0-18) skips
    # it even though simulation_mode is False on purpose.
    emulated = True

    def __init__(self, channel: ChannelModel, executor: SimExecutor,
                 ue_serving_gnb: Optional[Dict[str, str]] = None,
                 ue_rnti: Optional[Dict[str, int]] = None):
        self.channel = channel
        self.executor = executor
        self.ue_serving_gnb = dict(ue_serving_gnb or EMU_UE_SERVING)
        self.ue_rnti = dict(ue_rnti or EMU_UE_RNTI)
        gnbs = list(executor.states)
        self._neighbor = {g: next((o for o in gnbs if o != g), g)
                          for g in gnbs}
        self._last_tput: Dict[str, float] = {}
        # Batch F: accept the mandatory probe-config + shared-scheduler install
        # (the emulated throughput comes from the channel; the config is stored
        # for provenance so metadata matches what actually ran).
        self.probe_config = None
        self.scheduler = None

    def set_probe_config(self, probe_config) -> None:
        self.probe_config = probe_config

    def set_scheduler(self, scheduler) -> None:
        self.scheduler = scheduler

    def _throughput_map(self) -> Dict[str, float]:
        """Fresh per-UE throughput for every ATTACHED UE (P0-18 shared-cell
        aware). A cell serving >= 2 attached UEs uses the ChannelModel's
        NORMALIZED CONTENTION split (one cell draw partitioned by PF share, so
        raising one UE's priority lowers its cell-mates'); a single-UE cell uses
        the per-UE channel path UNCHANGED (identical draws/values to Batch A-F,
        which have one UE per cell). Detached / unconfigured UEs are omitted.

        Cells are visited in configured UE order so single-UE-cell draw order is
        byte-identical to the legacy per-UE loop."""
        attached_by_cell: Dict[str, List[Tuple[str, Optional[int]]]] = {}
        for ue, gnb in self.ue_serving_gnb.items():
            sim = self.executor.sim_gnbs.get(gnb)
            state = self.executor.states.get(gnb)
            if sim is None or state is None:
                continue
            rnti = self.ue_rnti.get(ue)
            attached = rnti in sim.ues if rnti is not None else True
            if not attached:
                continue
            attached_by_cell.setdefault(gnb, []).append((ue, rnti))
        out: Dict[str, float] = {}
        for gnb, ue_rntis in attached_by_cell.items():
            state = self.executor.states[gnb]
            other = self._neighbor[gnb]
            ostate = self.executor.states[other]
            if len(ue_rntis) >= 2:
                # SHARED cell: the contending UEs partition one bounded capacity
                # by PF share. The cell PRB cap bounds the shared capacity;
                # per-UE PRB caps are NOT partitioned here (documented P0-18
                # limit - the primary scenario partitions via sched priority).
                weights = {
                    ue: state.ue_sched_priority.get(rnti, state.sched_priority)
                    for ue, rnti in ue_rntis}
                shared = self.channel.contended_throughputs(
                    gnb, other, serving_off=state.power_offset_db,
                    neighbor_off=ostate.power_offset_db, weights=weights,
                    prb_cap=state.prb_cap)
                for ue, _ in ue_rntis:
                    out[ue] = shared[ue]
            else:
                ue, rnti = ue_rntis[0]
                # effective PRB cap: tighter of cell and per-UE (0 = uncapped)
                caps = [c for c in (state.prb_cap,
                                    state.ue_prb_cap.get(rnti, PRB_UNCAPPED))
                        if c and c > 0]
                eff_prb = min(caps) if caps else PRB_UNCAPPED
                weight = state.ue_sched_priority.get(rnti, state.sched_priority)
                out[ue] = self.channel.throughput(
                    gnb, other, serving_off=state.power_offset_db,
                    neighbor_off=ostate.power_offset_db,
                    prb_cap=eff_prb, pf_weight=weight)
        return out

    def collect_all(self) -> Dict[str, UEMetrics]:
        tputs = self._throughput_map()      # shared-cell aware (P0-18)
        out: Dict[str, UEMetrics] = {}
        for ue, gnb in self.ue_serving_gnb.items():
            sim = self.executor.sim_gnbs.get(gnb)
            state = self.executor.states.get(gnb)
            if sim is None or state is None:
                out[ue] = UEMetrics(ue_id=ue, attached=False,
                                    error="no emulated gNB")
                continue
            rnti = self.ue_rnti.get(ue)
            attached = rnti in sim.ues if rnti is not None else True
            if not attached:
                out[ue] = UEMetrics(ue_id=ue, attached=False)
                continue
            tput = tputs[ue]
            self._last_tput[ue] = tput
            out[ue] = UEMetrics(ue_id=ue, attached=True,
                                ip_address=f"10.0.0.{len(out) + 1}",
                                throughput_mbps=round(tput, 3),
                                rsrp=-80.0, sinr=20.0)
        return out

    def get_throughput_all(self, duration: float = 2.0) -> Dict[str, float]:
        """A GENUINE FRESH throughput probe (Batch F): recomputes each attached
        UE's throughput from the channel NOW - consuming an independent channel
        RNG draw per call - so repeated S4 probes are DISTINCT live measurements,
        never a cached repeat. Detached UEs are omitted (UNKNOWN upstream).
        Shared cells (P0-18) partition one bounded capacity by PF share."""
        tputs = self._throughput_map()
        out: Dict[str, float] = {}
        for ue in self.ue_serving_gnb:      # preserve configured UE order
            if ue in tputs:
                self._last_tput[ue] = tputs[ue]
                out[ue] = round(tputs[ue], 3)
        return out

    def shutdown(self):
        pass


# --------------------------------------------------------------------------- #
# Assembly                                                                     #
# --------------------------------------------------------------------------- #

def build_emulated_coordinator(seed: int = 12345, tau_trial_s: float = 0.5,
                               theta_mode: str = "adaptive", topology=None):
    """Real IntentCoordinator wired to the emulated RAN + mock LLM.

    `topology` (experiments.topology.ExperimentTopology) drives the emulated
    UE<->cell attachment DYNAMICALLY (P0-18: no hardcoded list, no fixed count).
    When None it DEFAULTS to the CURRENTLY PROVISIONED real UE set
    (provisioned_emulation_topology), which on today's testbed is UE1+UE2 both on
    gNB1 - a 2-UE SHARED cell - and AUTOMATICALLY grows to 3+ UEs if/when they are
    provisioned. The emulated default therefore tracks the real testbed, not a
    fixed 3-UE assumption. Pass an explicit topology to explore other scenarios:
    three_ue_shared_topology() for a future 3-UE what-if, or two_ue_topology()
    for the legacy single-UE-per-cell layout.

    Returns (coordinator, channel). Do NOT call coordinator.start(): the
    background metrics thread would consume channel RNG draws and break
    same-seed determinism.
    """
    from config import get_config
    from coordinator.intent_coordinator import IntentCoordinator
    if topology is None:
        # Default = the CURRENTLY PROVISIONED real UE set (config-derived), not a
        # hardcoded 3-UE topology. Today: UE1+UE2 share gNB1 (2-UE shared cell);
        # a provisioned UE3+ joins automatically.
        from experiments.topology import provisioned_emulation_topology
        topology = provisioned_emulation_topology(get_config().network)

    coordinator = IntentCoordinator(config=get_config())
    try:
        coordinator.ue_collector.shutdown()
    except Exception:
        pass

    channel = ChannelModel(seed=seed)
    # one SimTelnetGNB per configured cell; each UE's RNTI is added to its
    # serving cell (shared cells get MULTIPLE UEs, so scheduler priority is a
    # real per-UE lever there).
    sims = {}
    for i, bs in enumerate(topology.bs_ids()):
        sims[bs] = SimTelnetGNB(pci=i)
    for ue in topology.ue_ids():
        gnb = topology.serving_gnb(ue)
        rnti = topology.rnti(ue)
        if gnb in sims and rnti is not None:
            sims[gnb].add_ue(rnti)
    executor = SimExecutor(sims)
    collector = SimCollector(channel, executor,
                             ue_serving_gnb=dict(topology.ue_serving),
                             ue_rnti=dict(topology.ue_rnti))

    coordinator.executor = executor
    # P8: re-wire the RNTI-guard hooks onto the injected executor
    executor.rnti_resolver = coordinator._resolve_stale_rnti
    executor.on_rnti_remap = coordinator._on_rnti_remap
    coordinator.ue_collector = collector
    # H2: the emulated carrier is the 106-PRB profile (the ChannelModel's
    # PRB penalty term is calibrated against a 106-PRB carrier), while the
    # default config profile is 24 PRB - state the emulated bounds
    # explicitly so profile clamping cannot silently shrink the action set
    import dataclasses as _dc
    coordinator.action_space = _dc.replace(
        coordinator.action_space, prb_cap_max=106, ue_prb_cap_max=106)
    # keep the LLM-advertised bounds in sync with the installed space
    coordinator.llm_manager.action_space = coordinator.action_space
    coordinator.ue_serving_gnb = dict(topology.ue_serving)
    coordinator.ue_rnti = dict(topology.ue_rnti)
    # P0-18: the prompt enumerates the emulated topology's configured UEs
    coordinator.llm_manager.ue_ids = list(topology.ue_ids())
    coordinator.llm_manager.ue_serving = dict(topology.ue_serving)
    coordinator.tau_trial_s = float(tau_trial_s)
    coordinator.hard_failure_cap_s = 2.0

    if not coordinator.set_llm_backend("mock:deterministic"):
        raise RuntimeError("mock:deterministic backend not registered")
    mock = coordinator.llm_manager.dynamic_backends["mock:deterministic"]
    mock.reset(seed)

    if theta_mode == "fixed":
        coordinator.calibrator.mode = "fixed"
    # costs integrate tput over tau: scale the episode budget with the
    # shortened window so Eq. 9 keeps a meaningful magnitude
    coordinator.calibrator.c_episode *= float(tau_trial_s) / 15.0
    coordinator.calibrator.reset()
    return coordinator, channel
