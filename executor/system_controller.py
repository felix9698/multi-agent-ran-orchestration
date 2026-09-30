#!/usr/bin/env python3
"""
System Controller

Controls all components of the OAI 5G testbed:
- OAI 5G Core (Docker Compose)
- OAI gNBs (nr-softmodem) on PC1, PC2
- OAI NR-UEs (nr-uesoftmodem) on RPi1, RPi2

Allows starting/stopping the entire system from the GUI.
"""

import logging
import os
import re
import subprocess
import threading
import time
import socket
from dataclasses import dataclass, field
from typing import Dict, Optional, Callable, List
from enum import Enum

logger = logging.getLogger("SystemController")


class ComponentStatus(Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass
class ComponentConfig:
    """Configuration for a system component"""
    name: str
    hostname: str
    ssh_user: str
    start_cmd: str
    stop_cmd: str
    check_cmd: str
    is_local: bool = False
    tmux_session: str = ""
    working_dir: str = ""
    # P0-18 fail-closed: a component whose real launch identity is NOT
    # provisioned is NON-STARTABLE. start_component() refuses it SYNCHRONOUSLY
    # (returns False before any thread / SSH / _exec_cmd), so an unprovisioned UE
    # can never report a successful launch over a guessed identity.
    startable: bool = True


@dataclass
class SystemConfig:
    """Full system configuration"""
    core: ComponentConfig = None
    gnbs: Dict[str, ComponentConfig] = field(default_factory=dict)
    ues: Dict[str, ComponentConfig] = field(default_factory=dict)


class SystemController:
    """
    Controls all system components via SSH.

    Provides:
    - Individual component start/stop
    - Full system start/stop
    - Status monitoring
    - Log collection
    """

    # PRB configuration table with all constraints verified
    PRB_CONFIG = {
        # NOTE (2026-07-09): all profiles were moved from 3.55-3.62 GHz down to
        # SSB 3349.92 MHz (ARFCN 623328). The original frequencies sat inside
        # Korea's LIVE commercial n78 allocations (KT 3.5-3.6, SKT 3.6-3.7):
        # with real 3.5 GHz antennas the UEs camped on a commercial cell
        # (PCI 235) instead of ours, and the macro DL jammed our UL slots
        # (persistent Msg3 failures). 3.3-3.4 GHz has no commercial deployment.
        # Keep TX attenuation high (att_tx>=12) and range short (1-3 m) - this
        # is unlicensed lab use.
        # The two cells run on SEPARATE frequencies (gNB2 = +20.16 MHz):
        # the X310s have no GPS/external sync, so co-channel TDD cells drift
        # into frame collision (DL of one jams UL of the other -> links decay
        # and drop within minutes). Inter-frequency cells are stable AND make
        # UE->cell pairing deterministic: each UE is launched with its own
        # gNB's -C, so it can only find that cell (ue1->gnb1, ue2->gnb2).
        24: {
            "sample_rate": 15.36,      # MSps - fits 1GbE (< 25 MSps)
            "center_freq": 3349920000, # Hz gNB1 (= SSB center; UE1 -C)
            "center_freq_gnb2": 3370080000,  # Hz gNB2 (UE2 -C)
            # UE SSB subcarrier offset (gNB prints this as "OAI UE --ssb").
            # Default 516 is for 106 PRB; 24 PRB needs 24 or the UE never syncs.
            "ssb": 24,
            # -E (3/4 sampling -> 11.52 MSps) is NOT supported by nr-uesoftmodem
            # ("unknown sampling rate 11520000" - confirmed in testing). Run at 15.36.
            "needs_E": False,
            "min_interface": "1GbE",
            "gnb_conf_pc1": "gnb.sa.band78.fr1.24PRB.pc1.conf",
            "gnb_conf_pc2": "gnb.sa.band78.fr1.24PRB.pc2.conf",
        },
        51: {
            "sample_rate": 30.72,      # MSps - requires 10GbE on the gNB side
            "center_freq": 3349920000, # Hz gNB1 (= SSB center; UE1 -C)
            "center_freq_gnb2": 3370080000,  # Hz gNB2 (UE2 -C)
            "ssb": 186,                # verified via gNB "OAI UE --ssb" output
            "needs_E": True,           # UE at 23.04 MSps - RPi5 should sustain; monitor UE CPU
            "min_interface": "10GbE",
            "gnb_conf_pc1": "gnb.sa.band78.fr1.51PRB.pc1.conf",
            "gnb_conf_pc2": "gnb.sa.band78.fr1.51PRB.pc2.conf",
        },
        106: {
            "sample_rate": 61.44,      # MSps - requires 10GbE
            # NOTE: no center_freq_gnb2 - 2x 38.16 MHz carriers do not fit in
            # the commercial-free 3.3-3.4 GHz window, so 106 PRB is single-cell
            # only (both UEs on gNB1; gNB2 must stay off at this profile).
            "center_freq": 3349920000, # Hz (= SSB center; UE -C)
            "ssb": 516,                # verified via gNB "OAI UE --ssb" output
            "needs_E": True,           # UE at 46.08 MSps - borderline on RPi5 ARM; verify (watch late/CPU)
            "min_interface": "10GbE",
            "gnb_conf_pc1": "gnb.sa.band78.fr1.106PRB.pc1.conf",
            "gnb_conf_pc2": "gnb.sa.band78.fr1.106PRB.pc2.conf",
        },
    }

    # USIM credentials matching the CN database (docker-compose-basic-nrf.yaml
    # mounts database/oai_db2.sql). Subscribers 208950000000031/032 are
    # re-provisioned to slice SST=1 sd=0xFFFFFF dnn "oai" at core start
    # (see scripts/provision_subscribers.sh + core start_cmd) because:
    #   - the paper uses the standard eMBB slice (SST=1)
    #   - nr-uesoftmodem's conf validation rejects nssai_sst > 4, so the
    #     operator-specific SST=222 the DB ships with cannot be used in the
    #     UE's pdu_sessions block (PDU session -> no tun -> no throughput).
    # PDU session MUST come from a conf file (-O): the cmdline --uicc0.* args
    # set the USIM identity but leave the PDU session list empty.
    UE_UICC = {
        "ue1": {"imsi": "208950000000031"},
        "ue2": {"imsi": "208950000000032"},
    }
    UICC_DNN = "oai"
    UICC_SST = 1

    def __init__(self, config: SystemConfig = None, prb: int = 24,
                 network_config=None):
        self.prb = prb
        # P0-18: the INJECTED app network Config (config.Config) that drives ALL
        # UE/BS membership, per-UE serving cell (=> carrier), SSH identity and
        # IMSI. None => the process default (get_config()). Stored BEFORE
        # _create_config so component generation reads it.
        self._injected_network_config = network_config
        # When True, gNBs launch with the OAI soft-scope (-d) so the real Rx
        # constellation / channel-estimate window opens on each gNB's own
        # display. Requires libnrscope built and X access granted to root.
        self.scope_enabled = False
        # source-of-truth 10G/host-tuning script (pushed to PC2 by run_10g_setup)
        self.setup_10g_script = "/opt/ran-lab/controller/agentic_intent_coordinator/scripts/setup_10g.sh"
        self.config = config or self._create_config()
        self.status: Dict[str, ComponentStatus] = {}
        self._lock = threading.Lock()
        self._bulk_lock = threading.Lock()  # serializes start_all/stop_all

        # Status callbacks
        self.on_status_change: Optional[Callable[[str, ComponentStatus], None]] = None
        self.on_log: Optional[Callable[[str, str], None]] = None

        # Initialize status
        for name in self._all_components():
            self.status[name] = ComponentStatus.UNKNOWN

    def set_prb_config(self, prb: int):
        """Change PRB configuration (24 or 106)"""
        if prb not in self.PRB_CONFIG:
            logger.warning(f"Invalid PRB value: {prb}. Using 24.")
            prb = 24
        self.prb = prb
        self.config = self._create_config()
        logger.info(f"PRB configuration set to: {prb} ({self.PRB_CONFIG[prb]['min_interface']} required)")

    def set_scope(self, enabled: bool):
        """Enable/disable the OAI soft-scope on the gNBs. Takes effect on the
        next (re)start of each gNB."""
        self.scope_enabled = bool(enabled)
        self.config = self._create_config()
        logger.info(f"gNB soft-scope {'ENABLED' if enabled else 'disabled'} "
                    f"(applies on next gNB start)")

    def run_10g_setup(self, iface: str = "ens1") -> bool:
        """Apply 10G NIC + host real-time tuning and detect the USRP on both
        PCs, then update each gNB conf's sdr_addrs. Idempotent; meant to be
        run once after a reboot (the NIC/C-state settings are runtime-only).

        PC1: NOPASSWD wrapper /usr/local/sbin/oai-10g-setup.sh.
        PC2: NOPASSWD:ALL, script pushed to /tmp.
        """
        ok = True

        # --- PC1 (local) ---
        self._log("10g", "PC1: configuring 10G NIC + USRP + host tuning...")
        success, out, err = self._exec_cmd(
            self.config.gnbs["gnb1"],
            f"sudo -n /usr/local/sbin/oai-10g-setup.sh {iface}",
            timeout=90.0)
        tail = (out or err).strip().splitlines()[-3:] if (out or err) else []
        for line in tail:
            self._log("10g", f"PC1: {line}")
        if not success or "USRP found at" not in (out or ""):
            self._log("10g", "PC1: 10G setup did not detect a USRP (check SFP/power)")
            ok = False

        # --- PC2 (remote) ---
        gnb2 = self.config.gnbs["gnb2"]
        self._log("10g", "PC2: pushing script + configuring...")
        try:
            subprocess.run(
                ["scp", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                 self.setup_10g_script, f"{gnb2.ssh_user}@{gnb2.hostname}:/tmp/oai-10g-setup.sh"],
                capture_output=True, timeout=20.0)
        except Exception as e:
            self._log("10g", f"PC2: script copy failed: {e}")
        success, out, err = self._exec_cmd(
            gnb2, f"sudo bash /tmp/oai-10g-setup.sh {iface}", timeout=90.0)
        tail = (out or err).strip().splitlines()[-3:] if (out or err) else []
        for line in tail:
            self._log("10g", f"PC2: {line}")
        if not success or "USRP found at" not in (out or ""):
            self._log("10g", "PC2: 10G setup did not detect a USRP (check SFP/power)")
            ok = False

        self._log("10g", "10G setup complete" if ok else "10G setup finished with warnings")
        return ok

    def _create_config(self) -> SystemConfig:
        """Create configuration for OAI 5G testbed"""
        config = SystemConfig()

        # Get PRB-specific settings from table
        prb = self.prb
        prb_cfg = self.PRB_CONFIG.get(prb, self.PRB_CONFIG[24])

        gnb_conf_pc1 = prb_cfg["gnb_conf_pc1"]
        gnb_conf_pc2 = prb_cfg["gnb_conf_pc2"]
        ue_center_freq = prb_cfg["center_freq"]
        # UE2 tunes to gNB2's carrier (inter-frequency cells -> deterministic
        # ue1->gnb1 / ue2->gnb2 pairing). Falls back to gNB1's carrier at
        # profiles without a second carrier (106 PRB: single-cell only).
        ue2_center_freq = prb_cfg.get("center_freq_gnb2", ue_center_freq)
        ue_ssb = prb_cfg["ssb"]
        ue_extra_args = "-E" if prb_cfg["needs_E"] else ""

        def ue_conf_write(ue_name: str, imsi: str) -> str:
            """Shell snippet that writes the per-UE nr-uesoftmodem conf.

            The PDU session MUST be declared here (cmdline --uicc0.* leaves the
            PDU session list empty -> no data bearer). nssai_sst must be <= 4
            (OAI conf validation), hence SST=1. The IMSI comes from the injected
            config (P0-18), never a hardcoded per-UE table. Credential markers
            are filled only after validation at the start_component boundary;
            building or displaying this configuration needs no credentials.
            """
            path = f"/tmp/nrue_{ue_name}.conf"
            return (
                f"umask 077\ncat > {path} <<'UECONF'\n"
                f"uicc0 = {{\n"
                f'  imsi = "{imsi}";\n'
                '  key = "__AIC_UICC_KEY__";\n'
                '  opc = "__AIC_UICC_OPC__";\n'
                f'  pdu_sessions = ({{ dnn = "{self.UICC_DNN}"; nssai_sst = {self.UICC_SST}; }});\n'
                f"}}\n"
                f"UECONF"
            )

        def ue_conf_path(ue_name: str) -> str:
            return f"/tmp/nrue_{ue_name}.conf"

        # --telnetsrv enables the runtime control interface; the patched
        # "ci rfatt" command is the per-cell TX power knob.
        # Port 9091: leftover Open5GS services occupy 9090 on PC1 loopback,
        # and a telnet bind failure kills nr-softmodem entirely.
        gnb_extra_args = "--telnetsrv --telnetsrv.shrmod ci --telnetsrv.listenport 9091"

        # OAI soft-scope (-d): opens the real Rx constellation / channel window
        # on each gNB's OWN display. nr-softmodem runs as root, so root needs X
        # access (xhost) and DISPLAY in its env (env_keep in sudoers.d handles
        # DISPLAY). PC1 runs in the graphical session; PC2 is reached over SSH,
        # so it must locate the XWayland auth cookie and grant access first.
        if self.scope_enabled:
            scope_flag = " -d"
            xhost_local = "xhost +SI:localuser:root >/dev/null 2>&1; "
            disp = "DISPLAY=:0 "
            xhost_remote = (
                'XA=$(ls /run/user/1000/.mutter-Xwaylandauth.* 2>/dev/null | head -1); '
                'DISPLAY=:0 XAUTHORITY="$XA" xhost +SI:localuser:root >/dev/null 2>&1; ')
        else:
            scope_flag = ""
            xhost_local = xhost_remote = disp = ""

        # OAI paths on PC1 (local)
        oai_path_pc1 = "/opt/ran-lab/controller/openairinterface5g"
        oai_build_pc1 = f"{oai_path_pc1}/cmake_targets/ran_build/build"
        oai_conf_pc1 = f"{oai_path_pc1}/targets/PROJECTS/GENERIC-NR-5GC/CONF"

        # OAI paths on PC2
        oai_path_pc2 = "/opt/ran-lab/gnb2/openairinterface5g"
        oai_build_pc2 = f"{oai_path_pc2}/cmake_targets/ran_build/build"
        oai_conf_pc2 = f"{oai_path_pc2}/targets/PROJECTS/GENERIC-NR-5GC/CONF"

        # 5G Core (Docker on PC1)
        config.core = ComponentConfig(
            name="5g-core",
            hostname="localhost",
            ssh_user="ran-node1",
            is_local=True,
            working_dir="/opt/ran-lab/controller/oai-cn5g/docker-compose",
            # After the core is up, (re)apply the LAN->CN forwarding rules so
            # gNB2 on PC2 can reach the AMF/UPF. docker recreates its nftables
            # rules on every network create, so this must run each start.
            # Installed as NOPASSWD (sudoers.d/oai-cn-forward); '|| true' keeps
            # startup working on hosts where it isn't installed.
            start_cmd="cd /opt/ran-lab/controller/oai-cn5g/docker-compose && docker compose -f docker-compose-basic-nrf.yaml up -d && sleep 3 && (sudo -n /usr/local/sbin/oai-cn-forward.sh || true) && (bash /opt/ran-lab/controller/agentic_intent_coordinator/scripts/provision_subscribers.sh || true)",
            stop_cmd="cd /opt/ran-lab/controller/oai-cn5g/docker-compose && docker compose -f docker-compose-basic-nrf.yaml down",
            check_cmd="docker ps | grep -E 'oai-(amf|smf|upf)' | wc -l"
        )

        # gNB1 (PC1 - local, USRP)
        config.gnbs["gnb1"] = ComponentConfig(
            name="gnb1",
            hostname="localhost",
            ssh_user="ran-node1",
            is_local=True,
            tmux_session="gnb1",
            working_dir=oai_build_pc1,
            # cd /tmp: nr-softmodem writes nrMAC_stats.log/nrL1_stats.log into its
            # CWD; pin it so collectors know where to find them.
            start_cmd=f"""{xhost_local}tmux new-session -d -s gnb1 'cd /tmp && {disp}sudo {oai_build_pc1}/nr-softmodem -O {oai_conf_pc1}/{gnb_conf_pc1} --gNBs.[0].min_rxtxtime 6 {gnb_extra_args}{scope_flag} 2>&1 | tee /tmp/gnb1.log'""",
            # SIGTERM first (clean USRP release if it works), then SIGKILL any
            # survivor - OAI's SIGTERM handler often hangs during USRP teardown.
            # -x (exact process name) avoids pkill/pgrep matching this command's
            # own shell (whose cmdline contains "nr-softmodem").
            stop_cmd="tmux kill-session -t gnb1 2>/dev/null; sudo /usr/bin/pkill -x nr-softmodem 2>/dev/null; sleep 2; sudo /usr/bin/pkill -9 -x nr-softmodem 2>/dev/null",
            check_cmd="pgrep -x nr-softmodem | head -1"
        )

        # gNB2 (PC2 - remote via SSH, USRP)
        config.gnbs["gnb2"] = ComponentConfig(
            name="gnb2",
            hostname="192.168.0.51",
            ssh_user="ran-node2",
            is_local=False,
            tmux_session="gnb2",
            working_dir=oai_build_pc2,
            start_cmd=f"""{xhost_remote}tmux new-session -d -s gnb2 'cd /tmp && {disp}sudo {oai_build_pc2}/nr-softmodem -O {oai_conf_pc2}/{gnb_conf_pc2} --gNBs.[0].min_rxtxtime 6 {gnb_extra_args}{scope_flag} 2>&1 | tee /tmp/gnb2.log'""",
            stop_cmd="tmux kill-session -t gnb2 2>/dev/null; sudo /usr/bin/pkill -x nr-softmodem 2>/dev/null; sleep 2; sudo /usr/bin/pkill -9 -x nr-softmodem 2>/dev/null",
            check_cmd="pgrep -x nr-softmodem | head -1"
        )

        # UEs (P0-18): EVERY UE component is generated from the INJECTED network
        # configuration - membership, per-UE SERVING CELL (=> carrier), SSH
        # identity and IMSI all come from config, never a hardcoded ue1/ue2 pair
        # or a fixed ue1->gnb1 / ue2->gnb2 pairing. A UE whose physical identity
        # is NOT provisioned is made EXPLICITLY NON-STARTABLE: its start_cmd exits
        # non-zero so start() reports FAILURE - never a silent success over a
        # guessed host / IMSI / launch recipe.
        net = self._network_config()
        for ue_id, ue in net.ues.items():
            serving = getattr(ue, "initial_serving_gnb", "gnb1")
            # each UE tunes to ITS OWN serving cell's carrier (config-driven).
            cfreq = ue2_center_freq if serving == "gnb2" else ue_center_freq
            host = getattr(ue, "ip", "") or getattr(ue, "hostname", "")
            user = getattr(ue, "ssh_user", "")
            imsi = getattr(ue, "imsi", "")
            provisioned = bool(getattr(ue, "is_physically_provisioned", None)
                               and ue.is_physically_provisioned())
            if provisioned:
                build = (f"/home/{user}/openairinterface5g/cmake_targets/"
                         f"ran_build/build")
                start_cmd = (
                    "sudo sysctl -w kernel.sched_rt_runtime_us=990000 "
                    ">/dev/null 2>&1\n"
                    f"{ue_conf_write(ue_id, imsi)}\n"
                    "(command -v iperf3 >/dev/null && ! pgrep -x iperf3 "
                    ">/dev/null && taskset -c 0 iperf3 -s -D >/dev/null 2>&1)\n"
                    "(for i in $(seq 1 60); do ip -4 addr show oaitun_ue1 "
                    "2>/dev/null | grep -q 'inet ' && sudo ip route replace "
                    "192.168.70.128/26 dev oaitun_ue1 && break; sleep 2; done "
                    ">/dev/null 2>&1 &)\n"
                    f"tmux new-session -d -s {ue_id} 'sudo {build}/"
                    f"nr-uesoftmodem -O {ue_conf_path(ue_id)} -r {prb} "
                    f"--numerology 1 --band 78 -C {cfreq} --ssb {ue_ssb} "
                    f"--thread-pool 2,3 --agc {ue_extra_args} 2>&1 | "
                    f"tee /tmp/{ue_id}.log'")
            else:
                # FAIL CLOSED: an unprovisioned UE has no trusted launch identity.
                # The start_cmd exits NON-ZERO so start() can never report a
                # successful launch over a guessed host / IMSI.
                missing = (",".join(ue.unprovisioned_fields())
                           if getattr(ue, "unprovisioned_fields", None)
                           else "identity")
                build = ""
                start_cmd = (
                    f"echo 'UE {ue_id} on {serving} is UNPROVISIONED "
                    f"(missing: {missing}); refusing to launch over a guessed "
                    f"identity' >&2; exit 1")
            config.ues[ue_id] = ComponentConfig(
                name=ue_id, hostname=host, ssh_user=user, is_local=False,
                tmux_session=ue_id, working_dir=build, start_cmd=start_cmd,
                stop_cmd=(f"tmux kill-session -t {ue_id} 2>/dev/null; "
                          "sudo /usr/bin/pkill -x nr-uesoftmodem 2>/dev/null; "
                          "sleep 2; sudo /usr/bin/pkill -9 -x nr-uesoftmodem "
                          "2>/dev/null"),
                check_cmd="pgrep -x nr-uesoftmodem | head -1",
                startable=provisioned)   # unprovisioned => non-startable

        return config

    def _network_config(self):
        """The INJECTED/current app network configuration (P0-18) driving UE/BS
        membership. Falls back to the process default only when none was
        injected."""
        cfg = self._injected_network_config
        if cfg is None:
            from config import get_config
            cfg = get_config()
        return cfg.network

    def configured_ue_ids(self, config=None) -> List[str]:
        """The configured UE membership (P0-18), from the INJECTED/current app
        config (or an explicitly passed one) - never a hardcoded ue1/ue2 pair."""
        net = config.network if config is not None else self._network_config()
        return list(net.ues.keys())

    def _all_components(self) -> List[str]:
        """Get list of all component names"""
        names = []
        if self.config.core:
            names.append(self.config.core.name)
        names.extend(self.config.gnbs.keys())
        names.extend(self.config.ues.keys())
        return names

    def _get_component(self, name: str) -> Optional[ComponentConfig]:
        """Get component config by name"""
        if self.config.core and self.config.core.name == name:
            return self.config.core
        if name in self.config.gnbs:
            return self.config.gnbs[name]
        if name in self.config.ues:
            return self.config.ues[name]
        return None

    def _exec_cmd(self, comp: ComponentConfig, cmd: str, timeout: float = 30.0) -> tuple:
        """Execute command on component host"""
        try:
            if comp.is_local:
                result = subprocess.run(
                    ["bash", "-c", cmd],
                    capture_output=True, text=True, timeout=timeout
                )
            else:
                ssh_target = f"{comp.ssh_user}@{comp.hostname}"
                result = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                     ssh_target, cmd],
                    capture_output=True, text=True, timeout=timeout
                )
            return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
        except subprocess.TimeoutExpired:
            return False, "", "timeout"
        except Exception as e:
            return False, "", str(e)

    def _set_status(self, name: str, status: ComponentStatus):
        """Set component status and notify callback"""
        with self._lock:
            old_status = self.status.get(name)
            self.status[name] = status

        if old_status != status and self.on_status_change:
            self.on_status_change(name, status)

    def _log(self, component: str, message: str):
        """Log message and notify callback"""
        logger.info(f"[{component}] {message}")
        if self.on_log:
            self.on_log(component, message)

    def check_status(self, name: str) -> ComponentStatus:
        """Check if a component is running"""
        comp = self._get_component(name)
        if not comp:
            return ComponentStatus.UNKNOWN

        try:
            success, stdout, stderr = self._exec_cmd(comp, comp.check_cmd, timeout=5.0)

            if name == "5g-core":
                try:
                    count = int(stdout.strip()) if stdout.strip() else 0
                    return ComponentStatus.RUNNING if count >= 3 else ComponentStatus.STOPPED
                except:
                    return ComponentStatus.STOPPED
            else:
                if success and stdout.strip():
                    return ComponentStatus.RUNNING
                return ComponentStatus.STOPPED

        except Exception as e:
            logger.debug(f"Status check failed for {name}: {e}")
            return ComponentStatus.UNKNOWN

    def check_all_status(self) -> Dict[str, ComponentStatus]:
        """Check status of all components"""
        results = {}
        for name in self._all_components():
            results[name] = self.check_status(name)
            self._set_status(name, results[name])
        return results

    def start_component(self, name: str, async_start: bool = True) -> bool:
        """Start a single component"""
        comp = self._get_component(name)
        if not comp:
            self._log(name, f"Unknown component: {name}")
            return False
        if not getattr(comp, "startable", True):
            # P0-18 fail-closed: a NON-STARTABLE (unprovisioned) component is
            # refused SYNCHRONOUSLY - no thread, no SSH, no _exec_cmd, in BOTH
            # async and sync modes - so it can NEVER report a successful launch
            # over a guessed identity.
            self._set_status(name, ComponentStatus.ERROR)
            self._log(name, f"{name} is NOT startable (unprovisioned) - "
                            f"refusing to start")
            return False

        # Keep secrets out of persistent/displayable ComponentConfig objects.
        # Validate before spawning a thread or making any SSH/hardware call.
        start_cmd = comp.start_cmd
        uicc_values = []
        if name in self.config.ues:
            for variable in ("AIC_UICC_KEY", "AIC_UICC_OPC"):
                value = os.environ.get(variable, "")
                if not re.fullmatch(r"[0-9a-fA-F]{32}", value) or value == "0" * 32:
                    self._set_status(name, ComponentStatus.ERROR)
                    self._log(name, f"{variable} must be a nonzero 32-hex value; "
                                    "refusing to launch UE")
                    return False
                uicc_values.append(value)
                start_cmd = start_cmd.replace(f"__{variable}__", value)

        def do_start():
            self._set_status(name, ComponentStatus.STARTING)
            self._log(name, f"Starting {name}...")

            success, stdout, stderr = self._exec_cmd(comp, start_cmd, timeout=60.0)

            if success:
                time.sleep(3)
                status = self.check_status(name)
                if status == ComponentStatus.RUNNING:
                    self._set_status(name, ComponentStatus.RUNNING)
                    self._log(name, f"{name} started successfully")
                    return True
                else:
                    self._set_status(name, ComponentStatus.ERROR)
                    self._log(name, f"{name} start command succeeded but process not running")
                    return False
            else:
                self._set_status(name, ComponentStatus.ERROR)
                for value in uicc_values:
                    stderr = re.sub(re.escape(value), "[REDACTED]", stderr,
                                    flags=re.IGNORECASE)
                self._log(name, f"Failed to start {name}: {stderr}")
                return False

        if async_start:
            thread = threading.Thread(target=do_start, daemon=True)
            thread.start()
            return True
        else:
            return do_start()

    def stop_component(self, name: str, async_stop: bool = True) -> bool:
        """Stop a single component"""
        comp = self._get_component(name)
        if not comp:
            self._log(name, f"Unknown component: {name}")
            return False

        def do_stop():
            self._set_status(name, ComponentStatus.STOPPING)
            self._log(name, f"Stopping {name}...")

            success, stdout, stderr = self._exec_cmd(comp, comp.stop_cmd, timeout=30.0)

            time.sleep(2)
            status = self.check_status(name)

            if status == ComponentStatus.STOPPED:
                self._set_status(name, ComponentStatus.STOPPED)
                self._log(name, f"{name} stopped")
                return True
            else:
                self._set_status(name, ComponentStatus.ERROR)
                self._log(name, f"Failed to stop {name}")
                return False

        if async_stop:
            thread = threading.Thread(target=do_stop, daemon=True)
            thread.start()
            return True
        else:
            return do_stop()

    def start_all(self, async_start: bool = True):
        """Start all components in order: Core -> gNBs -> UEs"""
        def do_start_all():
            # Guard against double-click on "Start All" spawning parallel sequences
            if not self._bulk_lock.acquire(blocking=False):
                self._log("system", "Start/stop already in progress - ignoring")
                return
            try:
                self._log("system", "Starting full system...")

                # 1. Start Core
                if self.config.core:
                    self._log("system", "Step 1/3: Starting 5G Core...")
                    self.start_component(self.config.core.name, async_start=False)
                    time.sleep(5)

                # 2. Start gNBs
                self._log("system", "Step 2/3: Starting gNBs...")
                for gnb_name in self.config.gnbs.keys():
                    self.start_component(gnb_name, async_start=False)
                    time.sleep(3)

                time.sleep(5)

                # 3. Start UEs
                self._log("system", "Step 3/3: Starting UEs...")
                for ue_name in self.config.ues.keys():
                    self.start_component(ue_name, async_start=False)
                    time.sleep(2)

                self._log("system", "System startup complete")
            finally:
                self._bulk_lock.release()

        if async_start:
            thread = threading.Thread(target=do_start_all, daemon=True)
            thread.start()
        else:
            do_start_all()

    def stop_all(self, async_stop: bool = True):
        """Stop all components in reverse order: UEs -> gNBs -> Core"""
        def do_stop_all():
            if not self._bulk_lock.acquire(blocking=False):
                self._log("system", "Start/stop already in progress - ignoring")
                return
            try:
                self._log("system", "Stopping full system...")

                # 1. Stop UEs first
                self._log("system", "Step 1/3: Stopping UEs...")
                for ue_name in self.config.ues.keys():
                    self.stop_component(ue_name, async_stop=False)

                time.sleep(2)

                # 2. Stop gNBs
                self._log("system", "Step 2/3: Stopping gNBs...")
                for gnb_name in self.config.gnbs.keys():
                    self.stop_component(gnb_name, async_stop=False)

                time.sleep(2)

                # 3. Stop Core
                if self.config.core:
                    self._log("system", "Step 3/3: Stopping 5G Core...")
                    self.stop_component(self.config.core.name, async_stop=False)

                self._log("system", "System shutdown complete")
            finally:
                self._bulk_lock.release()

        if async_stop:
            thread = threading.Thread(target=do_stop_all, daemon=True)
            thread.start()
        else:
            do_stop_all()

    def get_component_log(self, name: str, lines: int = 50) -> str:
        """Get recent log output from a component.

        gNB/UE tee stdout to /tmp/<name>.log (local or over SSH). The 5G core
        is a docker stack, so aggregate the key NFs' container logs instead.
        """
        comp = self._get_component(name)
        if not comp:
            return ""

        if name == "5g-core":
            cmd = ("for c in oai-amf oai-smf oai-upf; do "
                   "echo \"===== $c =====\"; "
                   f"docker logs --tail {max(20, lines // 3)} $c 2>&1; done")
        else:
            log_file = f"/tmp/{name}.log"
            cmd = f"tail -{lines} {log_file} 2>/dev/null"

        success, stdout, stderr = self._exec_cmd(comp, cmd, timeout=8.0)
        return stdout if success else (stderr or "")

    def is_host_reachable(self, name: str) -> bool:
        """Check if component host is reachable via SSH"""
        comp = self._get_component(name)
        if not comp:
            return False

        if comp.is_local:
            return True

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex((comp.hostname, 22))
            sock.close()
            return result == 0
        except:
            return False
