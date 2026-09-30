#!/usr/bin/env python3
"""
Agentic Intent Coordinator Configuration

OAI 5G NR based Multi-UE, Multi-LLM setup for journal extension experiments.
Implements Extension Strategy specifications.

Testbed:
- OAI gNB (OpenAirInterface 5G NR)
- OAI 5G Core (AMF, SMF, UPF)
- OAI NR-UE or COTS UE with Quectel RM500Q
"""

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from enum import Enum


# SINGLE SOURCE OF TRUTH (P1-4): the declared FINITE achievable throughput ceiling
# (Mbps) a fully-effective in-bound coordination action can reach under the
# emulation/paper model. Both experiments.synthetic (as RECOVER_CEILING_MBPS) and
# experiments.metrics (as the U^ref reference-set) import THIS value, so they can
# never drift. It is a documented reference, NOT an unknown global optimum.
REFERENCE_CEILING_MBPS = 10.5

# LIVE / OTA ONLY: the I2 per-UE downlink throughput target (Mbps) used on the
# REAL testbed. This is a HARDWARE-DEPENDENT MEASURED parameter, NOT a paper
# constant, and it is deliberately SEPARATE from
# experiments.metrics.IntentConfig.throughput_target_mbps (8.0), which stays the
# offline synthetic/emulated paper-model default calibrated against
# REFERENCE_CEILING_MBPS above. Nothing offline reads this value.
#
# Measured 2026-07-29 on gNB1 (X310, 24 PRB, att_tx 18 dB, DL MCS cap [0..11])
# with RPi5 + B206mini UEs - full evidence in docs/ue_stability_and_capacity.md
# (§4 measured capacity, §5 I2 recommendation):
#   * one UE alone in the cell : 4.823 +/- 0.013 Mbps (n = 6)
#   * two UEs loaded together  : ~3.0 Mbps per UE, cell 6.026 +/- 0.016 (n = 3)
#   * the binding limit is the RPi5 UE's BASEBAND PROCESSING, not spectrum:
#     MCS 13 already costs ~5x the late-TX rate for ~12% throughput and MCS 15
#     kills the UE, hence dl_max_mcs = 11. There is NO headroom above 4.82.
#
# Why the offline 8.0 default is WRONG here: 8.0 Mbps is above the single-UE,
# zero-contention ceiling, so NO UE can satisfy I2 at ANY time. Strict
# satisfaction collapses to 0, R_success -> 0, and theta*'s premise
# C_worst > C_nego no longer holds - the run degenerates instead of exercising
# the coordinator.
#
# Why NOT 4.0-4.8: that band is satisfiable ONLY in the single-UE corner and
# leaves <= 0.8 Mbps of margin, which is inside the range the DL MCS outer loop
# wanders on its own (it needs 30-60 s to climb from MCS 0; §6.4). I2 would then
# be decided by outer-loop noise rather than by coordination.
#
# 3.5 Mbps sits on the "normally met, violated under load" boundary the operator
# asked for: +38% margin while the peer UE is idle (4.82) and -14% once the peer
# saturates the cell (3.0). I2 stays genuinely satisfiable AND genuinely
# violable, which is what a non-degenerate C_worst needs.
#
# RE-MEASURE AFTER ANY HARDWARE CHANGE (UE host, SDR, antenna repair, PRB count,
# MCS cap, att_tx). Until this constant is updated, override at run time with
# `python3 -m experiments.runner --mode live --i2-target-mbps <measured>` -
# never by editing the 8.0 offline default.
LIVE_I2_TARGET_MBPS = 3.5


class LLMBackend(Enum):
    """Supported LLM backends"""
    CLAUDE_SONNET = "claude-sonnet"
    GPT_4O = "gpt-4o"
    GPT_4O_MINI = "gpt-4o-mini"
    GEMINI_PRO = "gemini-pro"
    LLAMA_3 = "llama-3"
    PHI_3 = "phi-3"


@dataclass
class UEConfig:
    """UE Configuration for OAI NR-UE or COTS UE"""
    id: str                      # e.g., "ue1", "ue2"
    hostname: str                # e.g., "ran-ue1"
    ip: str                      # e.g., "192.168.0.52"
    ssh_user: str                # e.g., "ran-ue1"
    usrp_type: str               # e.g., "b206mini", "b210"
    initial_serving_gnb: str     # e.g., "gnb1"
    # Must exist in the CN DB actually mounted (oai_db2.sql)
    imsi: str = "208950000000031"

    # OAI NR-UE paths (if using OAI UE)
    oai_ue_bin: str = "/opt/oai/openairinterface5g/cmake_targets/ran_build/build/nr-uesoftmodem"
    oai_ue_conf: str = "/opt/oai/openairinterface5g/ci-scripts/conf_files/ue.conf"

    # COTS UE (if using Quectel module)
    quectel_device: str = "/dev/ttyUSB2"  # AT command interface

    def unprovisioned_fields(self) -> List[str]:
        """Physical-identity fields (host/IP/SSH/IMSI) that are still UNSET.

        A UE that is part of the logical topology but whose real hardware
        identity has NOT been supplied by the operator (config or UE<N>_* env)
        reports the missing fields here; guessed placeholders are not acceptable
        (handoff line 1181), so these must be filled from explicit operator
        input before any real-hardware collection/actuation."""
        return [name for name in ("hostname", "ip", "ssh_user", "imsi")
                if not str(getattr(self, name, "") or "").strip()]

    def is_physically_provisioned(self) -> bool:
        """True only when every physical-identity field is explicitly set."""
        return not self.unprovisioned_fields()


@dataclass
class GNBConfig:
    """gNB Configuration for OAI gNB"""
    id: str                      # e.g., "gnb1", "gnb2"
    hostname: str                # e.g., "ran-node1"
    ip: str                      # e.g., "192.168.0.50"
    ssh_user: str                # e.g., "ran-node1"
    pci: int                     # Physical Cell ID (NR-PCI)
    cell_id: int                 # NR Cell ID
    usrp_type: str = "x310"      # USRP type (x310, n310, b210)
    # USRP 10GbE address. The DEPLOYED value lives in the gNB conf's
    # sdr_addrs (auto-updated by scripts/setup_10g.sh); this default only
    # mirrors it for display (A3: one source of truth).
    usrp_addr: str = "192.168.40.2"

    # OAI specific paths
    oai_gnb_bin: str = "/opt/oai/openairinterface5g/cmake_targets/ran_build/build/nr-softmodem"
    oai_gnb_conf: str = "/opt/oai/openairinterface5g/targets/PROJECTS/GENERIC-NR-5GC/CONF/gnb.sa.band78.fr1.106PRB.usrpx310.conf"

    # OAI Telnet interface for runtime control
    # (9091: Open5GS leftovers occupy 9090 on PC1 loopback)
    telnet_ip: str = "127.0.0.1"
    telnet_port: int = 9091
    is_local: bool = False

    # NR parameters - defaults follow the 24 PRB / 1GbE profile.
    # SystemController.PRB_CONFIG + the deployed configs/oai/*.conf are the
    # SOURCE OF TRUTH for frequencies and profiles (A3); these fields only
    # mirror the gNB1 values for display. All profiles moved to the
    # commercial-free 3.3-3.4 GHz window on 2026-07-09 (gNB2 = +20.16 MHz;
    # see the PRB_CONFIG note in executor/system_controller.py).
    band: int = 78               # NR Band (n78)
    scs_khz: int = 30            # Subcarrier spacing (kHz)
    bandwidth_mhz: int = 10      # Bandwidth (MHz)
    num_prb: int = 24            # Number of PRBs
    dl_freq_hz: int = 3349920000 # DL center frequency (Hz, gNB1)
    ssb_freq_hz: int = 3349920000 # SSB frequency (Hz, gNB1)

    # Power control (runtime knob: TX attenuation offset via "ci rfatt")
    base_att_tx_db: int = 12     # baseline attenuation = 0 dB offset


@dataclass
class CoreConfig:
    """OAI 5G Core Configuration"""
    # Core network IPs (Docker network typically)
    amf_ip: str = "192.168.70.132"
    smf_ip: str = "192.168.70.133"
    upf_ip: str = "192.168.70.134"
    nrf_ip: str = "192.168.70.130"

    # N2/N3 interface (gNB facing)
    n2_ip: str = "192.168.70.132"  # AMF N2 interface
    n3_ip: str = "192.168.70.134"  # UPF N3 interface

    # Network slicing - subscribers re-provisioned to standard eMBB slice
    # (scripts/provision_subscribers.sh); nr-uesoftmodem rejects SST > 4.
    sst: int = 1                 # Slice/Service Type (eMBB)
    sd: str = "0xFFFFFF"         # Slice Differentiator (no SD)

    # DNN (Data Network Name)
    dnn: str = "oai"

    # PLMN - must match AMF plmn_support_list (basic_nrf_config.yaml)
    mcc: str = "208"
    mnc: str = "95"
    tac: int = 40960             # 0xa000


@dataclass
class ActionSpaceConfig:
    """
    Bounds U for the closed-loop action space (Extension Strategy: d>=2).

    Each axis is a runtime (no-restart) control knob on the RAN; the coordinator's
    enforcement layer clips every proposed value to these bounds before it touches
    hardware (Proposition 1: u_i <- min(max(u_i, u_i^min), u_i^max)).

    NEUTRAL DEFAULTS = backward compatibility: every new axis has a neutral value
    that reproduces the d=1 (power-only) baseline exactly. If the LLM omits an
    axis, the coordinator leaves it at neutral, so a proposal carrying only power
    offsets behaves identically to today.

        axis            neutral         effect of neutral
        power_offset    0.0 dB          TX at baseline attenuation (ci rfatt 12)
        prb             0 (uncapped)    scheduler may use the whole carrier
        sched_priority  1.0             plain proportional-fair (no bias)
        mcs_offset      0.0             MCS cap == base_mcs_cap (link adaptation
                                        unconstrained, as today)
    """
    # --- Axis 1: BS TX power offset (dB), via patched telnet "ci rfatt" ---
    power_offset_min_db: float = -10.0
    power_offset_max_db: float = 10.0
    power_offset_neutral: float = 0.0

    # --- Axis 2: DL PRB allocation cap (resource blocks), via new "ci prbcap" ---
    # 0 (== prb_uncapped) means "no cap": whole carrier, i.e. today's behavior.
    # A positive value caps each UE's per-slot DL PRB grant at runtime.
    prb_cap_min: int = 6
    prb_cap_max: int = 106
    prb_uncapped: int = 0            # neutral: full carrier bandwidth

    # --- Axis 3: DL scheduling priority (PF weight), via new "ci sched_prio" ---
    # 1.0 == neutral proportional-fair. >1 favors this cell's UE, <1 starves it.
    sched_priority_min: float = 0.25
    sched_priority_max: float = 4.0
    sched_priority_neutral: float = 1.0

    # --- Axis 4: DL MCS offset (relative to a base cap), via new "ci mcs" ---
    # Effective MCS cap = base_mcs_cap + offset, clipped to [0, base_mcs_cap].
    # offset 0 == cap at base_mcs_cap (unconstrained link adaptation, as today).
    # Negative offsets lower the ceiling (conservative link / degradation lever).
    mcs_offset_min: float = -20.0
    mcs_offset_max: float = 0.0
    mcs_offset_neutral: float = 0.0
    base_mcs_cap: int = 28          # OAI default dl_max_mcs (table 1 tops out at 27)

    # --- Per-UE (per-RNTI) addressing bounds ---
    # Whenever two or more UEs share a cell (CURRENT testbed: UE1+UE2 on gNB1),
    # intra-cell fairness intents need the LLM to address the PRB and
    # scheduling-priority knobs at the individual UE (RNTI), not just the whole
    # cell. This is driven by the live shared-cell membership (variable UE count),
    # not a fixed 3-UE assumption. Only these two axes are per-UE addressable:
    # power is an inherently
    # cell-wide RF property, and the per-UE MCS field (sched_ctrl->dl_max_mcs) is
    # clobbered by CSI, so both stay per-cell. The per-UE knobs are physically the
    # same as their per-gNB counterparts, so they default to identical bounds;
    # kept as separate fields so intra-cell partitioning can be retuned later
    # (e.g. a per-UE PRB share smaller than the whole carrier) without moving the
    # per-cell bounds.
    ue_prb_cap_min: int = 6
    ue_prb_cap_max: int = 106
    ue_sched_priority_min: float = 0.25
    ue_sched_priority_max: float = 4.0

    # Ordered list of the action-vector axes (c_t in R^d)
    AXES = ("power_offset", "prb", "sched_priority", "mcs_offset")
    # Axes that additionally support per-UE (per-RNTI) addressing
    PER_UE_AXES = ("prb", "sched_priority")

    def bounds(self, axis: str, per_ue: bool = False):
        """(min, max) bound for one axis (raises KeyError on unknown axis).

        With per_ue=True the per-UE variant of `axis` is returned; only the axes
        in PER_UE_AXES have one (KeyError otherwise).
        """
        if per_ue:
            return {
                "prb": (self.ue_prb_cap_min, self.ue_prb_cap_max),
                "sched_priority": (self.ue_sched_priority_min, self.ue_sched_priority_max),
            }[axis]
        return {
            "power_offset": (self.power_offset_min_db, self.power_offset_max_db),
            "prb": (self.prb_cap_min, self.prb_cap_max),
            "sched_priority": (self.sched_priority_min, self.sched_priority_max),
            "mcs_offset": (self.mcs_offset_min, self.mcs_offset_max),
        }[axis]

    def neutral(self, axis: str):
        """The value that makes `axis` a no-op (backward-compatible d=1 baseline).

        Per-UE neutrals equal the per-cell neutrals (uncapped PRB, PF weight 1.0),
        so an omitted per-UE axis leaves that UE untouched.
        """
        return {
            "power_offset": self.power_offset_neutral,
            "prb": self.prb_uncapped,
            "sched_priority": self.sched_priority_neutral,
            "mcs_offset": self.mcs_offset_neutral,
        }[axis]

    def clip(self, axis: str, value: float, per_ue: bool = False):
        """
        Clip a proposed value to axis U (enforcement layer). The PRB axis has a
        special "uncapped" sentinel (<=0 -> 0) that sits below prb_cap_min but is
        a legal neutral value, so it is handled explicitly rather than clamped up.
        per_ue selects the per-UE bounds for the PRB / scheduling-priority axes.
        """
        if axis == "prb":
            v = int(round(float(value)))
            if v <= 0:
                return self.prb_uncapped          # uncapped == full carrier
            lo, hi = self.bounds("prb", per_ue)
            return max(lo, min(hi, v))
        lo, hi = self.bounds(axis, per_ue)
        return max(lo, min(hi, float(value)))


@dataclass
class NetworkConfig:
    """Network Configuration"""
    # gNBs (5G NR base stations)
    gnbs: Dict[str, GNBConfig] = field(default_factory=dict)

    # User Equipments
    ues: Dict[str, UEConfig] = field(default_factory=dict)

    # 5G Core
    core: CoreConfig = field(default_factory=CoreConfig)

    # RF Parameters (NR Band n78) - 24 PRB / 1GbE default profile. Display
    # mirrors of SystemController.PRB_CONFIG (the source of truth, A3).
    band: int = 78
    dl_freq_mhz: float = 3349.92     # gNB1 carrier (gNB2 = 3370.08)
    scs_khz: int = 30
    bandwidth_mhz: int = 10

    # Handover Parameters (NR specific)
    a3_offset_db: float = 6.0    # A3 event offset
    hysteresis_db: float = 3.0   # Hysteresis
    ttt_ms: int = 100            # Time to trigger

    # Power-offset action space (dB, via patched "ci rfatt" telnet command).
    # Kept for backward compatibility; the full d>=2 action space lives in
    # `action_space` below (these two mirror its power-offset bounds).
    power_offset_min_db: float = -10.0
    power_offset_max_db: float = 10.0

    # Multi-dimensional action space U (Extension Strategy d>=2): power + PRB +
    # scheduling priority + MCS. The coordinator clips every proposed axis to
    # these bounds before applying it to the RAN.
    action_space: ActionSpaceConfig = field(default_factory=ActionSpaceConfig)


@dataclass
class LLMConfig:
    """
    LLM Backend Configuration

    API Keys / endpoints are loaded from environment variables (or a
    gitignored ./.env file - see load_dotenv_file):
    - ANTHROPIC_API_KEY: Claude models (claude-sonnet, claude-opus)
    - OPENAI_API_KEY: OpenAI models (gpt-4o, gpt-4o-mini)
    - GOOGLE_API_KEY: Gemini models (gemini-pro, gemini-flash)
    - LITELLM_BASE_URL / LITELLM_API_KEY: local LLM server behind a LiteLLM
      proxy (OpenAI-compatible). Models installed there are discovered
      dynamically and appear in the GUI dropdown as "local:<model>".

    Set these in your shell (or in ./.env, never committed):
        export ANTHROPIC_API_KEY="sk-ant-..."
        export OPENAI_API_KEY="sk-..."
        export GOOGLE_API_KEY="..."
        export LITELLM_BASE_URL="http://<tailscale-ip>:4000/v1"
        export LITELLM_API_KEY="sk-..."
    """
    backend: LLMBackend = LLMBackend.CLAUDE_SONNET

    # API Keys (loaded from environment variables)
    anthropic_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    google_api_key: Optional[str] = None

    # Ollama settings for local models (native API, localhost)
    ollama_base_url: str = "http://localhost:11434"

    # Local LLM server via a LiteLLM proxy (OpenAI-compatible, e.g. over Tailscale)
    litellm_base_url: Optional[str] = None
    litellm_api_key: Optional[str] = None

    # Model-specific settings
    temperature: float = 0.3
    max_tokens: int = 2048
    timeout_seconds: float = 30.0


@dataclass
class CalibrationConfig:
    """Adaptive Calibration Configuration (θ*, N_max).

    This is the ONE cold-start source (P0-12): initial_theta / initial_n_max are
    derived here from declared priors + deterministic caps, and the coordinator,
    synthetic, emulated and live modes all consume it. The EXPECTED cost
    estimates (c_worst/r_success/c_nego, empirical means used for θ* updates)
    are kept DISTINCT from the DETERMINISTIC safety caps used by the runtime
    reserve ledger (kpi_loss_cap_mbps / c_hard_cap / c_nego_cap), per P0-11.
    """
    # Confidence threshold θ* parameters (cold-start, DERIVED from cost priors
    # via compute_initial_theta(); the literal is only a legacy fallback).
    initial_theta: float = 0.7
    # Cold-start N_max (P0-12): None -> derived from the deterministic caps via
    # compute_initial_n_max(); an explicit int overrides (conservative policy).
    initial_n_max: Optional[int] = None

    # EXPECTED cost parameters (empirical means / priors) - feed θ* updates
    # ONLY, never the deterministic reserve ledger (P0-11).
    c_worst: float = 125.0       # Expected worst KPI degradation (Mbps·s)
    r_success: float = 64.5      # Expected utility from successful trial (Mbps·s)
    c_nego: float = 50.0         # Expected KPI loss during negotiation (Mbps·s)

    # Per-episode risk budget (the handoff/operator default). NOT inflated to
    # preserve negotiation rounds: under the HONEST deterministic caps (below,
    # c_hard_ub=300) this legitimately affords few/zero negotiation rounds, and
    # that conservative result is the REQUIRED safe behavior (blocker 6). An
    # experiment needing a larger cap must set an explicit override and record
    # it in provenance - never silently change this default.
    c_episode: float = 500.0     # Total cost operator allows per episode (Mbps·s)

    # Trial parameters
    tau_trial: float = 15.0      # Trial observation window (seconds)

    # Adaptive mode
    adaptive_theta: bool = True
    adaptive_n_max: bool = True

    # Hard failure parameters. P_HARD IS PER-TRIAL UNCONDITIONAL (P0-11): the
    # fraction of ALL trials that hard-fail, NOT a failure-conditional rate.
    p_hard: float = 0.02         # Per-trial unconditional hard-failure prob
    c_hard: float = 30.0         # Expected penalty for hard failure (Mbps·s)

    # --- DETERMINISTIC safety caps for the runtime reserve ledger (P0-11).
    # HONEST derivation (Gate-D blocker 6): every cost cap is derived from a
    # DECLARED FINITE NONNEGATIVE DURATION cap x the KPI-loss-rate cap (plus a
    # separately-named additive penalty when used), NOT an asserted number. So
    # c_hard_ub = kpi_loss_cap_mbps * hard_failure_cap_s (+ extra penalty) and
    # c_nego_ub = kpi_loss_cap_mbps * negotiation_duration_cap_s. ---
    kpi_loss_cap_mbps: float = 10.0        # worst sustained throughput deficit
    hard_failure_cap_s: float = 30.0       # declared max reconnection duration
    negotiation_duration_cap_s: float = 5.0  # declared max negotiation duration
    hard_failure_extra_penalty_mbps_s: float = 0.0  # named additive penalty
    # a stable action-space / profile provenance tag so the declared bound is
    # traceable in the cap/ledger audit (set by the coordinator from the active
    # ActionSpaceConfig; a label here for standalone use).
    action_space_profile: str = "default"

    # Larger operator budget so the HONEST deterministic caps still afford the
    # paper's negotiation rounds (with c_hard_ub=300 a 500 budget would allow
    # zero rounds); this is the declared per-episode risk budget.
    # (c_episode is declared above.)

    # Cold-start policy label (recorded in stats/evidence). "cost_derived" ->
    # values come from the caps/priors here; a conservative override documents
    # its reason/duration in metrics.
    cold_start_policy: str = "cost_derived"
    cold_start_policy_reason: str = ""

    def __post_init__(self):
        self._validate_caps()

    @staticmethod
    def _finite_nonneg(name: str, value: float) -> float:
        """Fail-closed: a cap must be a finite, nonnegative number."""
        try:
            f = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"CalibrationConfig.{name} is not a number: "
                             f"{value!r}")
        if not math.isfinite(f) or f < 0:
            raise ValueError(f"CalibrationConfig.{name} must be finite and "
                             f">= 0, got {f!r}")
        return f

    def _validate_caps(self) -> None:
        for name in ("kpi_loss_cap_mbps", "hard_failure_cap_s",
                     "negotiation_duration_cap_s",
                     "hard_failure_extra_penalty_mbps_s", "c_episode",
                     "tau_trial"):
            self._finite_nonneg(name, getattr(self, name))

    def compute_theta_star(self) -> float:
        """Compute confidence threshold θ* from EXPECTED cost structure (Eq. 8).

        Uses expected conditional quantities (means/priors), per P0-11 - this is
        a calibration estimate, NOT a deterministic safety bound."""
        denom = self.r_success + self.c_worst
        if denom <= 0:
            return float("nan")
        return (self.c_worst - self.c_nego) / denom

    # --- Deterministic upper bounds for the reserve ledger (P0-10 / P0-11) ---
    def c_hard_ub(self) -> float:
        """Deterministic worst-case hard-failure cost (Mbps·s): the worst
        throughput deficit sustained over the DECLARED reconnection-duration cap,
        plus any separately-named additive penalty. DERIVED from durations, not
        an asserted number (blocker 6)."""
        return (self.kpi_loss_cap_mbps * self.hard_failure_cap_s
                + self.hard_failure_extra_penalty_mbps_s)

    def c_trial_ub(self, tau_trial: Optional[float] = None) -> float:
        """Deterministic worst-case cost of ONE trial (Mbps·s): worst continuous
        KPI loss over the declared window PLUS the derived hard-failure cap. From
        declared duration caps only - never sample means."""
        tau = self.tau_trial if tau_trial is None else float(tau_trial)
        return self.kpi_loss_cap_mbps * max(0.0, tau) + self.c_hard_ub()

    def c_nego_ub(self) -> float:
        """Deterministic worst-case cost of ONE negotiation callback (Mbps·s):
        worst throughput deficit over the DECLARED negotiation-duration cap."""
        return self.kpi_loss_cap_mbps * self.negotiation_duration_cap_s

    def caps_audit(self, tau_trial: Optional[float] = None) -> Dict:
        """Traceable derivation of every deterministic cap (blocker 6): the
        declared duration caps, the KPI-loss rate, the action-space profile, and
        a stable content hash so the emitted bound is auditable."""
        tau = self.tau_trial if tau_trial is None else float(tau_trial)
        body = {
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
            "derivation": "cap = kpi_loss_cap_mbps * declared_duration_cap "
                          "(+ named additive penalty)",
        }
        import hashlib as _hashlib
        import json as _json
        body["provenance_hash"] = "caps-" + _hashlib.sha1(
            _json.dumps(body, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:12]
        return body

    def compute_n_max(self, tau_trial: Optional[float] = None) -> int:
        """Eq. 9 (corrected, P0-10):

            N_max = max(0, floor((C_episode - C_trial_ub)
                                 / (C_trial_ub + C_nego_ub)))

        over the DETERMINISTIC caps. If C_episode < C_trial_ub, 0 (not even the
        initial trial is affordable). N_max=0 therefore does NOT imply a free
        initial trial - the ledger separately reserves the initial trial."""
        t_ub = self.c_trial_ub(tau_trial)
        n_ub = self.c_nego_ub()
        if self.c_episode < t_ub:
            return 0
        denom = t_ub + n_ub
        if denom <= 0:
            return 0
        return max(0, int(math.floor((self.c_episode - t_ub) / denom)))

    # --- The ONE cold-start source consumed by every mode (P0-12) ---
    def compute_initial_theta(self) -> float:
        """Cold-start θ* every mode must start from (Eq. 8 over the priors)."""
        return self.compute_theta_star()

    def compute_initial_n_max(self, tau_trial: Optional[float] = None) -> int:
        """Cold-start N_max every mode must start from: the explicit override if
        set, else the deterministic corrected Eq. 9."""
        if self.initial_n_max is not None:
            return int(self.initial_n_max)
        return self.compute_n_max(tau_trial)

    def cold_start_source(self, tau_trial: Optional[float] = None) -> Dict:
        """The audit record of the cold-start policy/source (stats/evidence)."""
        return {
            "policy": self.cold_start_policy,
            "reason": self.cold_start_policy_reason,
            "initial_theta": self.compute_initial_theta(),
            "initial_n_max": self.compute_initial_n_max(tau_trial),
            "c_trial_ub": self.c_trial_ub(tau_trial),
            "c_nego_ub": self.c_nego_ub(),
            "c_episode": self.c_episode,
        }


@dataclass
class ExperimentConfig:
    """Experiment Configuration"""
    # Phase structure (5-phase scenario) - gains follow the journal paper
    # Table (serving BS1 / neighbor BS2): Nominal +3/0, Degradation -2/+1,
    # Impairment -5/+2, Recovery 0/0, Nominal +3/0. 300 s = 20 x 15 s steps.
    phases: List[Dict] = field(default_factory=lambda: [
        {"name": "Nominal", "duration_s": 60, "serving_gain": 3, "neighbor_gain": 0},
        {"name": "Degradation", "duration_s": 60, "serving_gain": -2, "neighbor_gain": 1},
        {"name": "Impairment", "duration_s": 60, "serving_gain": -5, "neighbor_gain": 2},
        {"name": "Recovery", "duration_s": 60, "serving_gain": 0, "neighbor_gain": 0},
        {"name": "Nominal", "duration_s": 60, "serving_gain": 3, "neighbor_gain": 0},
    ])

    # Trial parameters
    num_trials: int = 5
    kpi_interval_s: float = 15.0

    # Baseline methods to compare
    methods: List[str] = field(default_factory=lambda: [
        "rule_based",
        "score_heuristic",
        "rl_controller",
        "llm_no_history",
        "llm_with_history"
    ])

    # Output
    output_dir: str = "experiment_results"

    # Batch E (P0-15): real history ablation controls. history_policy is the
    # with-history treatment ("frozen" reads a controlled snapshot and never
    # learns; "online" learns finalized outcomes). history_reset_scope is how
    # often the reservoir is reset ("run"/"method"/"trial"/"model"). Validated
    # by coordinator.history.HistoryPolicy / HistoryResetScope in the runner.
    history_policy: str = "online"
    history_reset_scope: str = "trial"

    # Batch F (P0-14): EXPLICIT throughput-probe configuration recorded on every
    # result/sample/evidence. offered_load_mbps is the iperf UDP offered load (no
    # longer a hidden 12 Mbps constant); probe_mode distinguishes GOODPUT (rate
    # AT the offered load) from CAPACITY (saturating). A CAPACITY probe requires
    # offered_load >= capacity_saturation_floor_mbps or it fails closed.
    offered_load_mbps: float = 12.0
    probe_protocol: str = "udp"
    probe_direction: str = "downlink"
    probe_duration_s: float = 2.0
    probe_mode: str = "goodput"
    capacity_saturation_floor_mbps: float = 100.0

    def __post_init__(self):
        # fail-closed numeric validation (reject bool/NaN/inf/negative/zero as
        # applicable) via the Batch F measurement validators.
        from coordinator.measurement import (
            _finite_positive, ProbeMode, MeasurementError)
        self.offered_load_mbps = _finite_positive(
            "offered_load_mbps", self.offered_load_mbps)
        self.probe_duration_s = _finite_positive(
            "probe_duration_s", self.probe_duration_s)
        self.capacity_saturation_floor_mbps = _finite_positive(
            "capacity_saturation_floor_mbps",
            self.capacity_saturation_floor_mbps)
        self.kpi_interval_s = _finite_positive(
            "kpi_interval_s", self.kpi_interval_s)
        if isinstance(self.num_trials, bool) or \
                not isinstance(self.num_trials, int) or self.num_trials <= 0:
            raise MeasurementError(
                f"num_trials must be a positive int, got {self.num_trials!r}")
        ProbeMode.coerce(self.probe_mode)          # fail-closed on invalid mode
        # fail-closed to the ONLY implemented probe combination (udp+downlink);
        # ProbeConfig raises on anything unsupported.
        self.probe_config()

    def probe_config(self):
        """The validated ProbeConfig for this experiment (P0-14)."""
        from coordinator.measurement import ProbeConfig
        return ProbeConfig(offered_load_mbps=self.offered_load_mbps,
                           protocol=self.probe_protocol,
                           direction=self.probe_direction,
                           duration_s=self.probe_duration_s,
                           mode=self.probe_mode)


# --------------------------------------------------------------------------- #
# §1-4 (Sec I-IV) load-driven phase structure                                 #
# --------------------------------------------------------------------------- #
#
# The legacy `ExperimentConfig.phases` above is the §V-B (Sec V) channel-gain
# sweep (Nominal +3/0 ... Impairment -5/+2 ... Recovery). The operator has
# INVALIDATED Sec V (unrevised draft), so that dB table is NOT a design basis
# and, separately, cannot be honestly actuated on this testbed (no external
# attenuator; `ci rfatt` is a COORDINATOR ACTION axis). See
# docs/paper_alignment_sec1_4.md.
#
# What §1-4 grounds instead:
#   * The exogenous environment is a RAN OPERATING CONDITION - "cell load,
#     interference, scheduler behavior" (§I) and x_k = "traffic load, UE
#     associations, radio measurements, and available RAN resources" (§II-B).
#     The conflict is driven by RESOURCE COMPETITION, not a channel script.
#   * One conflict episode: active J1 = UE throughput >= 8 Mbit/s (Eq.4),
#     arriving J2 = powerConsumption <= P_th (Fig 1); Tx-power conflict (Fig 1).
#     NOTE: 8 Mbit/s is the PAPER's own number, quoted here as a citation. The
#     value this repo actually enforces is IntentConfig.throughput_target_mbps
#     (8.0 offline; LIVE_I2_TARGET_MBPS on the real testbed, where 8 Mbit/s is
#     physically unreachable - see that constant's comment above).
#   * The coordination loop is THREE-fold: Analysis / Execution / Negotiation
#     (§III; Fig 2 layers), with three terminal states (Eq.12).
#
# Honest driver on this testbed = offered traffic load (iperf3), which modulates
# shared-cell resource competition (channel gains stay 0). The default THREE
# regimes each primarily exercise ONE §1-4 coordination layer:
#   0 Nominal    (moderate load)  -> Analysis  (clearly-satisfiable admits)
#   1 Congestion (saturating load)-> Execution + Negotiation (trials/rollback/S5)
#   2 Recovery   (moderate load)  -> [Cont.] drift monitor returns to S0
#
# NOTE: §1-4 does not literally enumerate a phase COUNT (that was the §V-B
# table). Three is the minimal structure that exercises no-conflict -> conflict
# -> return and maps onto §1-4's three coordination layers; it is a design
# choice motivated by §1-4, not a §1-4 mandate. The count and the load levels
# (calibrated live in Phase-B) are parameters, not fixed truths. See OQ-SEC14-*.

def sec1_4_load_phases(nominal_mbps: float = REFERENCE_CEILING_MBPS,
                       congested_mbps: float = 2.0 * REFERENCE_CEILING_MBPS
                       ) -> List[Dict]:
    """The §1-4-grounded load-driven phase structure (channel gains == 0).

    `nominal_mbps` offers each UE ~the achievable ceiling so the LINK, not the
    traffic source, is the limit and both intents are satisfiable. `congested_mbps`
    (default 2x nominal) saturates the shared cell so resource competition pushes
    per-UE goodput below the CONFIGURED I2 target - the conflict the coordinator
    must resolve within J2's power cap. That target is NOT a fixed 8 Mbit/s: it is
    `IntentConfig.throughput_target_mbps`, i.e. 8.0 for the offline paper model
    (ceiling REFERENCE_CEILING_MBPS = 10.5) and LIVE_I2_TARGET_MBPS on the real
    24-PRB testbed, whose measured per-UE ceiling is 4.82 Mbps solo / ~3.0 Mbps
    under 2-UE contention. Every phase carries ONLY environment axes
    (offered load + zero channel gains); it never carries a coordinator action
    knob, so it passes experiments.environment.phase_environment_request and can
    be driven by an OfferedLoadEnvironmentDriver. Load levels are Phase-B
    calibration parameters, not fixed results."""
    from coordinator.measurement import _finite_positive
    nominal = _finite_positive("nominal_mbps", nominal_mbps)
    congested = _finite_positive("congested_mbps", congested_mbps)
    return [
        {"name": "Nominal", "duration_s": 60, "serving_gain": 0,
         "neighbor_gain": 0, "offered_load_mbps": nominal},
        {"name": "Congestion", "duration_s": 60, "serving_gain": 0,
         "neighbor_gain": 0, "offered_load_mbps": congested},
        {"name": "Recovery", "duration_s": 60, "serving_gain": 0,
         "neighbor_gain": 0, "offered_load_mbps": nominal},
    ]


@dataclass
class Config:
    """Main Configuration"""
    network: NetworkConfig = field(default_factory=NetworkConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    calibration: CalibrationConfig = field(default_factory=CalibrationConfig)
    experiment: ExperimentConfig = field(default_factory=ExperimentConfig)

    # True = single-host RFsim development (no SSH/hardware access);
    # False = real testbed collection (RPis, gNB logs, iperf3 via core)
    simulation_mode: bool = False

    # GUI settings
    gui_refresh_ms: int = 500
    gui_theme: str = "dark"

    def live_ue_partition(self):
        """Split the configured UEs into (active_provisioned_ids,
        {unprovisioned_id: missing_fields}) for the LIVE path.

        UE COUNT IS VARIABLE. A UE that carries an EXPLICIT operator-provided
        physical identity (host / IP / SSH user / IMSI) is ACTIVE; one whose
        identity was never supplied (e.g. UE3 before the third RPi is
        provisioned) is a LOGICAL EXPANSION SLOT. Pure: no raise, no mutation."""
        active, excluded = [], {}
        for ue_id, ue in self.network.ues.items():
            if ue.is_physically_provisioned():
                active.append(ue_id)
            else:
                excluded[ue_id] = ue.unprovisioned_fields()
        return active, excluded

    def validate_live_topology(self):
        """Fail-closed gate before REAL-hardware collection/actuation (P0-18),
        WITH expansion-slot tolerance.

        UNPROVISIONED UEs are logical expansion slots, NOT a fatal error: they are
        excluded from the live topology (the caller trims collection/experiment/
        GUI to the returned active set) and are NEVER run against a guessed
        placeholder. This method fails closed ONLY when NO UE is provisioned at
        all - the genuine misconfiguration where there is nothing real to run.

        Returns the ordered list of ACTIVE (provisioned) UE ids. Emulated / RFsim-
        simulation paths do NOT call this (no real hardware); it guards only the
        live startup path."""
        active, excluded = self.live_ue_partition()
        if not active:
            detail = "; ".join(f"{u} missing {', '.join(f)}"
                               for u, f in excluded.items()) \
                or "no UEs configured"
            raise ValueError(
                "live topology refused: NO UE has an explicit operator-provided "
                "physical identity (set via config or UE<N>_HOST/IP/SSH_USER/"
                f"IMSI env) - {detail}. At least one provisioned UE is required; "
                "guessed placeholders are not acceptable (handoff line 1181).")
        return active


def load_dotenv_file(path: str = None) -> None:
    """
    Minimal .env loader (no external dependency). Reads KEY=VALUE lines from a
    gitignored ./.env next to this file and sets them in os.environ *only if not
    already set*, so explicit shell exports always win. Keeps the GUI one-click:
    fill ./.env once instead of exporting keys every launch. See ./env.example.
    """
    import os
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.lower().startswith("export "):
                    line = line[7:]
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if val[:1] in ('"', "'"):
                    # Quoted value: take the content between the matching quotes
                    # and ignore anything after (e.g. a trailing inline comment).
                    # A quoted value may legitimately contain '#'.
                    q = val[0]
                    end = val.find(q, 1)
                    val = val[1:end] if end != -1 else val[1:]
                else:
                    # Unquoted value: strip an inline '   # comment'.
                    hpos = val.find(" #")
                    if hpos != -1:
                        val = val[:hpos]
                    val = val.strip()
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        # A malformed .env must never crash startup.
        pass


def get_default_config() -> Config:
    """Get default configuration for OAI 5G NR testbed"""
    import os

    # Load ./.env (if present) before reading any environment variables.
    load_dotenv_file()

    config = Config()

    # gNBs (OAI 5G NR) - identities must match the deployed
    # gnb.sa.band78.fr1.*PRB.pc{1,2}.conf files
    config.network.gnbs = {
        "gnb1": GNBConfig(
            id="gnb1",
            hostname="localhost",
            ip="192.168.0.50",
            ssh_user="ran-node1",
            pci=0,
            cell_id=12345678,
            usrp_type="x310",
            # PC1's NI-2954R (replaced the 2944R) answers at .40.4, not the
            # .40.2 the 2944R used. Display mirror; conf sdr_addrs rules.
            usrp_addr="192.168.40.4",
            is_local=True,
        ),
        "gnb2": GNBConfig(
            id="gnb2",
            hostname="192.168.0.51",
            ip="192.168.0.51",
            ssh_user="ran-node2",
            pci=1,
            cell_id=87654321,
            usrp_type="x310",
            # PC2's NI-2974 carries an internal PC that occupies the only 10 GbE
            # port under the HG image, so it was reflashed to XG; the front port
            # then comes up at .30.2. Display mirror; conf sdr_addrs rules.
            usrp_addr="192.168.30.2",
            is_local=False,
        )
    }

    # User Equipments (RPi5 + USRP B206mini). UE COUNT IS VARIABLE - this dict is
    # the SINGLE source of truth and every consumer reads it dynamically
    # (experiments.topology.from_network_config), never a hardcoded count.
    # CURRENT TESTBED FACT (2026-07): two provisioned UEs, UE1 and UE2, both
    # attach to gNB1 and GENUINELY CONTEND for its cell resource (a 2-UE SHARED
    # cell / intra-cell fairness scenario). UE3 below is a LOGICAL entry for a
    # PLANNED expansion (a 3rd RPi -> gNB2), NOT present hardware: its host/IP/
    # IMSI are UNSET until an operator provisions them, so is_physically_
    # provisioned() is False and validate_live_topology() fails it closed out of
    # every real path. It auto-joins the topology the moment it is provisioned.
    # See docs/ue_count_variability.md. (The §V-B "3-UE topology" derivation is
    # invalid basis - §I-IV impose no UE count/placement, per
    # docs/paper_alignment_sec1_4.md.)
    config.network.ues = {
        "ue1": UEConfig(
            id="ue1",
            hostname="192.168.0.52",
            ip="192.168.0.52",
            ssh_user="ran-ue1",
            usrp_type="b206mini",
            initial_serving_gnb="gnb1",
            imsi="208950000000031"
        ),
        "ue2": UEConfig(
            id="ue2",
            hostname="192.168.0.53",
            ip="192.168.0.53",
            ssh_user="ran-ue2",
            usrp_type="b206mini",
            initial_serving_gnb="gnb1",   # shares gNB1's cell with UE1 (P0-18)
            imsi="208950000000032"
        ),
        # UE3 is part of the REQUIRED logical primary topology (-> gNB2), but its
        # real hardware identity is NOT guessed (handoff line 1181): host / IP /
        # SSH user / IMSI come ONLY from explicit operator input via the
        # UE3_HOST / UE3_IP / UE3_SSH_USER / UE3_IMSI environment variables (or
        # ./.env). When absent they stay UNSET (""), and
        # Config.validate_live_topology() fails closed before any real-hardware
        # collection/actuation - never a placeholder that looks provisioned.
        "ue3": UEConfig(
            id="ue3",
            hostname=os.environ.get("UE3_HOST", ""),
            ip=os.environ.get("UE3_IP", ""),
            ssh_user=os.environ.get("UE3_SSH_USER", ""),
            usrp_type=os.environ.get("UE3_USRP_TYPE", "b206mini"),
            initial_serving_gnb="gnb2",
            imsi=os.environ.get("UE3_IMSI", "")
        )
    }

    # Optional operator-declared LIVE UE subset (Phase-B ergonomics for the
    # R4 live-start topology gate). validate_live_topology() requires EVERY
    # configured UE to be physically provisioned; on a testbed with fewer
    # physical UEs than the full logical topology (e.g. the current 2-UE
    # bring-up: UE1+UE2 on gNB1, no 3rd RPi), set LIVE_UE_SET to the
    # comma-separated ids ACTUALLY present (e.g. "ue1,ue2") to run against
    # exactly those. This is an EXPLICIT operator declaration - not a guessed
    # placeholder - so it preserves the fail-closed safety intent while
    # unblocking the run. Unset (the default) keeps the full 3-UE logical
    # topology unchanged. Every consumer reads config.network.ues dynamically
    # (experiments.topology.from_network_config, the coordinator, the GUI), so
    # trimming this dict trims the whole live topology consistently.
    _ue_set = os.environ.get("LIVE_UE_SET", "").strip()
    if _ue_set:
        wanted = [u.strip() for u in _ue_set.split(",") if u.strip()]
        unknown = [u for u in wanted if u not in config.network.ues]
        if unknown:
            raise ValueError(
                f"LIVE_UE_SET names unknown UE(s) {unknown}; configured UEs "
                f"are {list(config.network.ues)}")
        config.network.ues = {u: config.network.ues[u] for u in wanted}

    # 5G Core (OAI CN5G) - values match docker-compose-basic-nrf.yaml
    # and the mounted oai_db2.sql subscriber database
    config.network.core = CoreConfig()

    # LLM API keys / endpoints from environment
    config.llm.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    config.llm.openai_api_key = os.environ.get("OPENAI_API_KEY")
    config.llm.google_api_key = os.environ.get("GOOGLE_API_KEY")
    config.llm.litellm_base_url = os.environ.get("LITELLM_BASE_URL")
    config.llm.litellm_api_key = os.environ.get("LITELLM_API_KEY")

    return config


# Singleton config instance
_config: Optional[Config] = None

def get_config() -> Config:
    """Get global config instance"""
    global _config
    if _config is None:
        _config = get_default_config()
    return _config

def set_config(config: Config):
    """Set global config instance"""
    global _config
    _config = config
