#!/usr/bin/env python3
"""
Multi-UE Metrics Collector (OAI 5G NR)

Collects per-UE metrics for the OAI testbed:
- UE process / attach state via SSH to the Raspberry Pi UEs
  (nr-uesoftmodem, tun device oaitun_ue1)
- RSRP / UL-SNR / MCS / BLER / CQI parsed from the serving gNB's stdout log
  (dump_mac_stats blocks in /tmp/gnb{1,2}.log; one UE per gNB by placement,
  so the newest stats block belongs to that gNB's UE)
- Latency via ping from the UE through the RAN to the ext-dn container
- DL throughput via iperf3 UDP pushed FROM the oai-ext-dn container (on PC1)
  TO the UE's tun IP (iperf3 -s runs on each RPi, started by SystemController)

Topology constants match docker-compose-basic-nrf.yaml:
  UE subnet 12.1.1.0/24, UPF gateway 12.1.1.1, ext-dn 192.168.70.135.
"""

import logging
import socket
import subprocess
import time
import re
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from threading import Lock

logger = logging.getLogger("MultiUECollector")

# OAI CN5G data-plane landmarks
EXT_DN_IP = "192.168.70.135"     # oai-ext-dn container (answers ping, runs iperf3 client)
EXT_DN_CONTAINER = "oai-ext-dn"
UE_TUN_DEV = "oaitun_ue1"        # OAI NR-UE PDU session tun device

# Default hardware layout (PC1 runs this code)
DEFAULT_UE_HW = {
    "ue1": {"host": "192.168.0.52", "ssh_user": "ran-ue1", "gnb": "gnb1"},
    "ue2": {"host": "192.168.0.53", "ssh_user": "ran-ue2", "gnb": "gnb2"},
}
# Where each gNB's stdout log lives (gnb1 local on PC1, gnb2 via SSH)
GNB_LOG_ACCESS = {
    "gnb1": {"local": True, "log": "/tmp/gnb1.log", "pci": 0},
    "gnb2": {"local": False, "host": "192.168.0.51", "ssh_user": "ran-node2",
             "log": "/tmp/gnb2.log", "pci": 1},
}
# gNB telnet endpoints for the patched "ci stats" JSON snapshot (P9 / A2
# wiring). Matches executor.oai_executor: gnb1 local on PC1, gnb2 on PC2.
GNB_TELNET = {
    "gnb1": ("127.0.0.1", 9091),
    "gnb2": ("192.168.0.51", 9091),
}


@dataclass
class CellInfo:
    """Serving cell information"""
    pci: int
    rsrp: float
    rsrq: float = 0.0
    sinr: float = 0.0
    arfcn: int = 0


@dataclass
class UEMetrics:
    """Per-UE metrics"""
    ue_id: str
    timestamp: datetime = field(default_factory=datetime.now)

    # Connection state
    attached: bool = False
    ip_address: Optional[str] = None

    # Serving cell
    serving_cell: Optional[CellInfo] = None
    neighbor_cells: List[CellInfo] = field(default_factory=list)

    # Performance metrics
    throughput_mbps: Optional[float] = None
    latency_ms: Optional[float] = None
    packet_loss_percent: Optional[float] = None

    # Radio metrics (from serving gNB MAC stats)
    rsrp: Optional[float] = None
    rsrq: Optional[float] = None
    sinr: Optional[float] = None
    cqi: Optional[int] = None
    mcs: Optional[int] = None
    dl_bler: Optional[float] = None

    # Batch F (P0-14): the HONEST provenance of the radio KPIs above. The
    # default hardware source is the serving gNB's UL MAC stats (average RSRP /
    # ULSCH SNR), which is NOT the paper's UE-DL SS-RSRP / DL-SINR. honest_radio_
    # kpis() emits fields whose NAMES reveal the true source+direction; the
    # legacy rsrp/sinr attributes remain only as a labelled compatibility view.
    radio_source: str = "gnb_ul_mac"     # gnb_ul_mac | ue_dl_meas_report
    radio_direction: str = "uplink"      # uplink (gNB UL) | downlink (UE DL)

    # Collection metadata
    collection_latency_ms: float = 0.0
    error: Optional[str] = None

    def honest_radio_kpis(self) -> Dict:
        """Radio KPIs under HONEST field names that reveal the actual source and
        direction (P0-14). For the gNB-UL-MAC source the SNR is emitted as
        ``gnb_ul_snr_db`` and the RSRP as ``gnb_ul_avg_rsrp_dbm`` (NOT UE-DL SINR /
        SS-RSRP). If the paper's UE-DL metrics are the real source they are named
        ``ue_dl_sinr_db`` / ``ue_dl_ss_rsrp_dbm``; otherwise those are UNKNOWN
        (None) rather than fabricated from the gNB-UL numbers."""
        src = self.radio_source
        out = {"radio_source": src, "radio_direction": self.radio_direction}
        if src == "ue_dl_meas_report":
            out["ue_dl_sinr_db"] = self.sinr
            out["ue_dl_ss_rsrp_dbm"] = self.rsrp
        else:                                    # gnb_ul_mac (default)
            out["gnb_ul_snr_db"] = self.sinr
            out["gnb_ul_avg_rsrp_dbm"] = self.rsrp
            # the paper's UE-DL metrics are NOT available from this source:
            # emit UNKNOWN rather than mislabel the gNB-UL numbers as UE-DL.
            out["ue_dl_sinr_db"] = None
            out["ue_dl_ss_rsrp_dbm"] = None
        # CQI / MCS / BLER are gNB-REPORTED DL-scheduler values (from the gNB
        # MAC log), NOT UE-measured DL KPIs - name them so the source+direction
        # is visible (review P0-14 A), never bare cqi/mcs/dl_bler.
        out["gnb_reported_dl_cqi_index"] = self.cqi
        out["gnb_dl_mcs_index"] = self.mcs
        out["gnb_dl_bler_ratio"] = self.dl_bler
        return out

    def to_dict(self) -> Dict:
        # Batch F (P0-14, coordinator review #8): the DEFAULT/public export uses
        # HONEST radio names (gnb_ul_avg_rsrp_dbm / gnb_ul_snr_db + source/direction
        # for the gNB-UL source; ue_dl_* only when that is the true source). The
        # ambiguous rsrp/sinr keys are NOT top-level - they live only under an
        # explicit ``legacy_radio`` adapter that names their real source, so a
        # consumer can never mistake a gNB-UL value for a UE-DL metric.
        d = {
            "ue_id": self.ue_id,
            "timestamp": self.timestamp.isoformat(),
            "attached": self.attached,
            "ip_address": self.ip_address,
            "serving_pci": self.serving_cell.pci if self.serving_cell else None,
            "throughput_mbps": self.throughput_mbps,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }
        d.update(self.honest_radio_kpis())         # honest names + provenance
        # Batch F (review C): the ambiguous legacy names are NOT in the default
        # export - a consumer that explicitly needs them calls
        # legacy_radio_adapter() (opt-in), so raw/CSV can never leak rsrp/sinr.
        return d

    def legacy_radio_adapter(self) -> Dict:
        """The EXPLICIT legacy view of the ambiguous radio fields, each tagged
        with its ACTUAL source/direction (Batch F). Consumers that still need the
        old ``rsrp``/``sinr``/``rsrq`` names read them from here, where the
        provenance makes clear they are NOT UE-DL SS-RSRP / DL-SINR."""
        return {
            "rsrp": self.rsrp,
            "rsrq": self.rsrq,
            "sinr": self.sinr,
            "cqi": self.cqi,
            "mcs": self.mcs,
            "dl_bler": self.dl_bler,
            "source": self.radio_source,
            "direction": self.radio_direction,
            "note": ("legacy names; actual source is "
                     f"{self.radio_source} ({self.radio_direction})"),
        }


def _run(cmd: List[str], timeout: float) -> Tuple[bool, str]:
    """Run a local command, return (ok, stdout)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as e:
        return False, str(e)


def parse_gnb_mac_stats(log_tail: str, metrics: "UEMetrics", pci: int,
                        rnti: str = None):
    """Parse the newest dump_mac_stats block from a gNB stdout log.

    OAI prints per-UE blocks like:
      UE RNTI b23e CU-UE-ID 1 in-sync PH 52 dB PCMAX 21 dBm, average RSRP -77 (16 meas)
      UE b23e: CQI 13, RI 1, PMI (0,0)
      UE b23e: dlsch_rounds 523/3/0/0, dlsch_errors 0, ..., BLER 0.00374 MCS (1) 24
      UE b23e: ulsch_rounds 597/0/0/0, ..., NPRB 5  SNR 30.0 dB

    When two UEs share one gNB the blocks interleave, so pass the UE's own
    C-RNTI (from its log: "UE 0 RNTI 716a stats") to select only its lines.
    """
    lines = log_tail.splitlines()
    if rnti:
        pat = re.compile(rf"\b{re.escape(rnti)}\b", re.IGNORECASE)
        lines = [l for l in lines if pat.search(l)]
    for line in reversed(lines):
        if metrics.rsrp is None and "average RSRP" in line:
            m = re.search(r"UE RNTI\s+([0-9a-fA-F]+).*?(in-sync|out-of-sync).*?"
                          r"average RSRP\s+(-?\d+)", line)
            if m:
                metrics.attached = metrics.attached or (m.group(2) == "in-sync")
                metrics.rsrp = float(m.group(3))
                metrics.serving_cell = CellInfo(pci=pci, rsrp=metrics.rsrp,
                                                sinr=metrics.sinr or 0.0)
        elif metrics.sinr is None and "ulsch_rounds" in line:
            m = re.search(r"SNR\s+([-\d.]+)", line)
            if m:
                metrics.sinr = float(m.group(1))
                if metrics.serving_cell:
                    metrics.serving_cell.sinr = metrics.sinr
        elif ((metrics.dl_bler is None or metrics.mcs is None)
              and "dlsch_rounds" in line):
            # independent guards: a ci-stats snapshot may have filled only
            # one of the two (A2) - fill the missing one from the log
            # without overwriting the structured value
            m = re.search(r"BLER\s+([\d.]+)\s+MCS\s+\(\d+\)\s+(\d+)", line)
            if m:
                if metrics.dl_bler is None:
                    metrics.dl_bler = float(m.group(1))
                if metrics.mcs is None:
                    metrics.mcs = int(m.group(2))
        elif metrics.cqi is None and re.search(r"UE [0-9a-fA-F]+: CQI", line):
            m = re.search(r"CQI\s+(\d+)", line)
            if m:
                metrics.cqi = int(m.group(1))
        if (metrics.rsrp is not None and metrics.sinr is not None
                and metrics.dl_bler is not None and metrics.mcs is not None
                and metrics.cqi is not None):
            break


class LocalUECollector:
    """Simulation-mode collector (single-host RFsim: everything on PC1)"""

    def __init__(self, ue_id: str, log_file: str = None):
        self.ue_id = ue_id
        self.ue_log_file = log_file or f"/tmp/{ue_id}.log"
        self.gnb_log_file = "/tmp/gnb1.log"
        self._last_metrics: Optional[UEMetrics] = None
        self._lock = Lock()

    def collect(self) -> UEMetrics:
        start_time = time.time()
        metrics = UEMetrics(ue_id=self.ue_id)

        try:
            gnb_running, _ = _run(["pgrep", "-f", "nr-softmodem"], 3)
            ue_running, _ = _run(["pgrep", "-f", "nr-uesoftmodem"], 3)
            if not gnb_running:
                metrics.error = "gNB not running"
                return metrics
            if not ue_running:
                metrics.error = "UE not running"
                return metrics

            try:
                with open(self.gnb_log_file, 'r') as f:
                    tail = "".join(f.readlines()[-80:])
                parse_gnb_mac_stats(tail, metrics, pci=0)
            except FileNotFoundError:
                pass

            try:
                with open(self.ue_log_file, 'r') as f:
                    output = "".join(f.readlines()[-30:])
                if ('RRC_CONNECTED' in output
                        or 'Contention Resolution is successful' in output):
                    metrics.attached = True
            except FileNotFoundError:
                pass

        except Exception as e:
            metrics.error = str(e)
            logger.error(f"Error collecting from {self.ue_id}: {e}")

        metrics.collection_latency_ms = (time.time() - start_time) * 1000
        with self._lock:
            self._last_metrics = metrics
        return metrics

    def get_throughput(self, duration: float = 2.0,
                       direction: str = "downlink") -> Optional[float]:
        """No traffic path in simulation mode."""
        return None

    def get_goodput_passive(self, duration: float = 2.0,
                            direction: str = "downlink") -> Optional[float]:
        """No tun-counter path in simulation mode (passive goodput is hardware-
        only; the emulated/RFsim path models throughput analytically)."""
        return None

    def get_last_metrics(self) -> Optional[UEMetrics]:
        with self._lock:
            return self._last_metrics


class UECollector:
    """Hardware-mode collector for one OAI NR-UE on a Raspberry Pi."""

    def __init__(self, ue_id: str, hostname: str, ssh_user: str,
                 gnb_id: str = None):
        self.ue_id = ue_id
        self.hostname = hostname
        self.ssh_user = ssh_user
        self.ssh_target = f"{ssh_user}@{hostname}"
        # Static placement mapping - used only as a fallback. The actual
        # serving gNB is discovered per collection from the UE's own log
        # (decoded PCI), because with OTA cell selection the UE attaches to
        # whichever SSB is strongest, not to the gNB we planned on paper.
        self.gnb_id = gnb_id or DEFAULT_UE_HW.get(ue_id, {}).get("gnb", "gnb1")
        self.gnb_access = GNB_LOG_ACCESS.get(self.gnb_id, GNB_LOG_ACCESS["gnb1"])
        self._pci_to_access = {a["pci"]: (g, a) for g, a in GNB_LOG_ACCESS.items()}

        self._last_metrics: Optional[UEMetrics] = None
        self._lock = Lock()

    def _ssh_cmd(self, cmd: str, timeout: float = 5.0) -> Tuple[bool, str]:
        return _run(["ssh", "-o", "ConnectTimeout=3",
                     "-o", "StrictHostKeyChecking=no",
                     self.ssh_target, cmd], timeout)

    def _discover_serving(self) -> Tuple[Optional[int], Optional[str]]:
        """Read the UE's own log for its decoded serving PCI and C-RNTI.

        The UE prints "BCCH update: phyCellID 0->1" on camping and
        "UE 0 RNTI 716a stats" periodically. PCI selects which gNB's log to
        parse; RNTI selects this UE's lines when two UEs share a gNB.
        """
        ok, out = self._ssh_cmd(
            f"grep -aoP 'phyCellID \\d+->\\K\\d+' /tmp/{self.ue_id}.log 2>/dev/null | tail -1;"
            f" grep -aoP 'UE 0 RNTI \\K[0-9a-fA-F]{{4}}' /tmp/{self.ue_id}.log 2>/dev/null | tail -1",
            timeout=5)
        if not ok or not out:
            return None, None
        parts = out.split()
        pci = int(parts[0]) if parts and parts[0].isdigit() else None
        rnti = parts[1] if len(parts) > 1 else None
        return pci, rnti

    def _gnb_ci_stats(self, gnb_id: str) -> Optional[Dict]:
        """JSON MAC snapshot from the serving gNB's patched `ci stats`
        telnet command (P9), or None (unpatched gNB / transport failure).
        Gate B [A2]: this wires the structured-observability path into the
        MAIN closed-loop telemetry - callers fall back to stdout-log
        parsing when it is unavailable.
        """
        from collectors.gnb_collector import GNBCollector
        endpoint = GNB_TELNET.get(gnb_id)
        if endpoint is None:
            return None
        host, port = endpoint
        def _read_until_prompt(s) -> bytes:
            # TCP-framing-safe: keep reading until the OAI prompt (or
            # timeout/EOF) - a single recv may return a partial line, and a
            # partially-drained banner would make the NEXT read stop on the
            # leftover prompt before the JSON arrives
            data = b""
            while len(data) < 65536:
                try:
                    chunk = s.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                data += chunk
                if data.rstrip(b" ").endswith(b">"):
                    break
            return data

        try:
            with socket.create_connection((host, port), timeout=2.0) as sock:
                sock.settimeout(2.0)
                _read_until_prompt(sock)     # banner + FULL first prompt
                sock.sendall(b"ci stats\n")
                out = _read_until_prompt(sock).decode(errors="replace")
        except OSError:
            return None
        return GNBCollector.parse_ci_stats(out)

    @staticmethod
    def _apply_ci_stats(metrics: UEMetrics, stats: Dict, rnti: str) -> bool:
        """Fill this UE's MAC-visible fields (MCS, DL BLER) from a
        `ci stats` snapshot, selecting the row by C-RNTI. Returns True when
        the UE's row was found. RSRP/SINR/CQI stay with the log path - out
        of the patch's scope (see oai_patches/README.md)."""
        if not rnti:
            return False
        try:
            want = int(rnti, 16)
        except (TypeError, ValueError):
            return False
        for ue in stats.get("ues") or []:
            if not isinstance(ue, dict):
                continue
            r = ue.get("rnti")
            try:
                have = int(r, 16) if isinstance(r, str) else int(r)
            except (TypeError, ValueError):
                continue
            if have != want:
                continue
            if isinstance(ue.get("dl_mcs"), (int, float)):
                metrics.mcs = int(ue["dl_mcs"])
            if isinstance(ue.get("dl_bler"), (int, float)):
                metrics.dl_bler = float(ue["dl_bler"])
            return True
        return False

    def _gnb_log_tail(self, access: Dict = None, lines: int = 240) -> Optional[str]:
        """Tail a gNB's stdout log (local file or over SSH)."""
        access = access or self.gnb_access
        if access.get("local"):
            try:
                with open(access["log"], 'r') as f:
                    return "".join(f.readlines()[-lines:])
            except FileNotFoundError:
                return None
        target = f"{access['ssh_user']}@{access['host']}"
        ok, out = _run(["ssh", "-o", "ConnectTimeout=3",
                        "-o", "StrictHostKeyChecking=no", target,
                        f"tail -{lines} {access['log']} 2>/dev/null"], 6)
        return out if ok and out else None

    def collect(self) -> UEMetrics:
        start_time = time.time()
        metrics = UEMetrics(ue_id=self.ue_id)

        try:
            # 1. UE process on the RPi
            success, _ = self._ssh_cmd("pgrep -f nr-uesoftmodem")
            if not success:
                metrics.error = "nr-uesoftmodem not running"
                return metrics

            # 2. PDU session tun device -> attached + UE IP.
            #    MUST filter on `show up`: when the UE is released from the cell
            #    (RRCRelease -> SDAP "bringing TUN oaitun_ue1 down"), OAI takes
            #    the LINK down but LEAVES THE IPv4 ADDRESS CONFIGURED. A plain
            #    `ip addr show` therefore still prints an inet line for a UE
            #    that is no longer on any cell and can carry no traffic, so the
            #    UE reads as attached while its goodput is structurally 0.
            #    Observed on the live testbed (docs/ue_stability_and_capacity.md):
            #      attached  -> <POINTOPOINT,NOARP,UP,LOWER_UP>  flags 0x91
            #      released  -> <POINTOPOINT,NOARP>              flags 0x90,
            #                   inet 12.1.1.x/24 STILL PRESENT
            #    `show up` is the exact discriminator (verified both ways).
            success, output = self._ssh_cmd(
                f"ip -4 -o addr show up dev {UE_TUN_DEV} 2>/dev/null"
                " | grep 'inet ' | awk '{print $4}' | cut -d/ -f1")
            if success and output:
                metrics.attached = True
                metrics.ip_address = output.splitlines()[0].strip()

            # 3. Radio metrics from the serving gNB's MAC stats.
            #    Discover the ACTUAL serving cell from the UE's own log (PCI)
            #    and disambiguate multi-UE gNB logs by this UE's C-RNTI.
            pci, rnti = self._discover_serving()
            discovered = self._pci_to_access.get(pci) if pci is not None else None
            access = discovered[1] if discovered else self.gnb_access
            access_gnb = discovered[0] if discovered else self.gnb_id
            # A2: STRUCTURED telemetry first - the patched `ci stats` JSON
            # provides this UE's MCS/BLER without regex-scraping stdout.
            # The log tail still runs afterwards for RSRP/SINR/CQI (out of
            # the patch's scope) and is the full fallback on unpatched
            # gNBs; parse_gnb_mac_stats only fills fields still None, so
            # the JSON values are never overwritten.
            stats = self._gnb_ci_stats(access_gnb) if rnti else None
            if stats is not None:
                self._apply_ci_stats(metrics, stats, rnti)
            tail = self._gnb_log_tail(access)
            if tail:
                parse_gnb_mac_stats(tail, metrics,
                                    pci=pci if pci is not None
                                    else access.get("pci", 0),
                                    rnti=rnti)

            # 4. RAN latency: UE -> ext-dn through the tun device
            if metrics.ip_address:
                success, output = self._ssh_cmd(
                    f"ping -c 1 -W 1 -I {UE_TUN_DEV} {EXT_DN_IP} 2>/dev/null"
                    " | grep 'time='")
                if success and output:
                    m = re.search(r'time[=<]([\d.]+)', output)
                    if m:
                        metrics.latency_ms = float(m.group(1))

        except Exception as e:
            metrics.error = str(e)
            logger.error(f"Error collecting from {self.ue_id}: {e}")

        metrics.collection_latency_ms = (time.time() - start_time) * 1000
        with self._lock:
            self._last_metrics = metrics
        return metrics

    def get_throughput(self, duration: float = 2.0,
                       direction: str = "downlink",
                       udp_rate: str = "12M") -> Optional[float]:
        """Measure DL throughput: iperf3 UDP from ext-dn (PC1 docker) to the UE.

        Runs the client inside the oai-ext-dn container LOCALLY on PC1 - the
        iperf3 server runs on the RPi (started by SystemController). Returns
        loss-adjusted received rate in Mbps, or None if the link can't complete
        the transfer.

        IMPORTANT: iperf3 is wrapped in `timeout` INSIDE the container. A plain
        `docker exec ... iperf3` that we time out from the host kills only the
        host-side exec, orphaning the container-side iperf3; those orphans pile
        up over repeated polls until the client fails with "Bad file descriptor"
        and wedges every later measurement. The in-container `timeout` guarantees
        the client process dies with its measurement, so nothing accumulates.
        """
        last = self.get_last_metrics()
        ue_ip = last.ip_address if last else None
        if not ue_ip:
            return None

        hard = int(duration) + 6
        cmd = ["docker", "exec", EXT_DN_CONTAINER, "timeout", str(hard),
               "iperf3", "-c", ue_ip, "-u", "-b", udp_rate,
               "-t", str(int(duration)), "--connect-timeout", "3000", "-J"]
        ok, output = _run(cmd, hard + 4)
        if not output:
            return None
        try:
            data = json.loads(output)
            end = data.get("end", {})
            summ = end.get("sum", {}) or end.get("sum_sent", {})
            bps = summ.get("bits_per_second", 0.0)
            loss = summ.get("lost_percent", 0.0)
            received_mbps = bps * (1.0 - loss / 100.0) / 1e6
            if last:
                last.throughput_mbps = received_mbps
                last.packet_loss_percent = loss
            return received_mbps
        except (json.JSONDecodeError, TypeError):
            return None

    def _read_tun_rx_bytes(self) -> Optional[int]:
        """Read the UE tun device's cumulative DL received-byte counter.

        A PASSIVE read (``cat /sys/class/net/oaitun_ue1/statistics/rx_bytes``):
        it injects NO traffic and never touches the UE's iperf3 server, so it
        cannot collide with the background offered load. Returns the counter (a
        monotonically increasing non-negative int) or None if it can't be read.
        """
        ok, out = self._ssh_cmd(
            f"cat /sys/class/net/{UE_TUN_DEV}/statistics/rx_bytes 2>/dev/null")
        if not ok or not out:
            return None
        first = out.strip().splitlines()[0].strip() if out.strip() else ""
        try:
            val = int(first)
        except (TypeError, ValueError):
            return None
        return val if val >= 0 else None

    def get_goodput_passive(self, duration: float = 2.0,
                            direction: str = "downlink") -> Optional[float]:
        """Measure DL goodput PASSIVELY from the UE tun ``rx_bytes`` counter delta
        over ``duration`` seconds (loss-adjusted delivered rate in Mbps).

        Unlike get_throughput this injects NO iperf3 session and never touches the
        UE's single iperf3 server, so it CANNOT collide with the background
        offered load. (Two iperf3 clients on one server make the server reject /
        100%-drop one of them, which get_throughput then reports as a spurious
        0.00 - the live measurement defect this fixes.) The counter delta is the
        ACTUAL bytes the RAN delivered to the UE in the window, i.e. the honest
        "throughput under the offered load" - so under congestion it reads the
        real reduced goodput, not zero, and it has NO artificial ceiling (a
        higher MCS delivers more bytes -> a higher reading).

        Returns None (an explicit no-measurement, never a fake 0) when the counter
        cannot be read at both ends of the window or it resets mid-window - so the
        caller settles UNKNOWN rather than a fabricated value.
        """
        dur = float(duration)
        if not (dur > 0.0):                     # rejects 0 / negative / NaN
            return None
        b0 = self._read_tun_rx_bytes()
        if b0 is None:
            return None
        time.sleep(dur)                          # measurement window (load flows)
        b1 = self._read_tun_rx_bytes()
        if b1 is None:
            return None
        delta = b1 - b0
        if delta < 0:                            # counter reset / device re-created
            return None
        return (delta * 8.0) / (dur * 1e6)

    def get_last_metrics(self) -> Optional[UEMetrics]:
        with self._lock:
            return self._last_metrics


class MultiUECollector:
    """
    Collects metrics from multiple UEs in parallel.

    simulation_mode=True  -> parse local logs only (single-host RFsim dev)
    simulation_mode=False -> full hardware collection (SSH to RPis, gNB logs,
                             iperf3 through the core)
    """

    def __init__(self, ue_configs: Dict[str, Dict] = None,
                 simulation_mode: bool = False):
        self.collectors: Dict[str, object] = {}
        self.simulation_mode = simulation_mode
        self._executor = ThreadPoolExecutor(max_workers=4)
        self._ue_configs = ue_configs or {
            ue_id: dict(cfg) for ue_id, cfg in DEFAULT_UE_HW.items()
        }
        # Batch F (P0-13/P0-14): the SHARED measurement scheduler (set by the
        # coordinator so background + trial probes share ONE collection lock) and
        # the explicit probe config (offered load / protocol / direction /
        # duration / mode). Defaults keep standalone use working.
        self.scheduler = None
        self.probe_config = None
        self._build_collectors()

    @classmethod
    def from_config(cls, config=None, simulation_mode: bool = False):
        """Build a collector whose UE MEMBERSHIP and serving-cell mapping are
        DERIVED FROM CONFIGURATION (P0-18) - every configured UE (ue1, ue2, ue3,
        ...) is enumerated with its config serving gNB, so adding a UE in config
        (e.g. UE3 on gNB2) is picked up WITHOUT editing this module's hardcoded
        default table. Physical host/SSH details come from the UE's config
        identity when set, else the hardware default table; an unprovisioned UE
        is still enumerated so the live provisioning check can fail closed."""
        if config is None:
            from config import get_config
            config = get_config()
        ue_configs: Dict[str, Dict] = {}
        for ue_id, ue in config.network.ues.items():
            hw = DEFAULT_UE_HW.get(ue_id, {})
            ue_configs[ue_id] = {
                "host": (getattr(ue, "ip", "") or getattr(ue, "hostname", "")
                         or hw.get("host")),
                "ssh_user": getattr(ue, "ssh_user", "") or hw.get("ssh_user"),
                "gnb": getattr(ue, "initial_serving_gnb", "") or hw.get("gnb"),
                "imsi": getattr(ue, "imsi", ""),
            }
        return cls(ue_configs=ue_configs, simulation_mode=simulation_mode)

    def set_scheduler(self, scheduler) -> None:
        """Install the SHARED MeasurementScheduler (both background and trial
        throughput probes acquire it, so overlapping iperf cannot run)."""
        self.scheduler = scheduler

    def set_probe_config(self, probe_config) -> None:
        """Install the explicit ProbeConfig (offered load / mode / etc.)."""
        self.probe_config = probe_config

    def _udp_rate_arg(self) -> str:
        cfg = self.probe_config
        return cfg.udp_rate_arg() if cfg is not None else "12M"

    def _build_collectors(self):
        self.collectors.clear()
        for ue_id, cfg in self._ue_configs.items():
            if self.simulation_mode:
                self.add_local_ue(ue_id)
            else:
                host = cfg.get("host") or cfg.get("hostname") \
                    or DEFAULT_UE_HW.get(ue_id, {}).get("host")
                user = cfg.get("ssh_user") \
                    or DEFAULT_UE_HW.get(ue_id, {}).get("ssh_user")
                gnb = cfg.get("gnb") \
                    or DEFAULT_UE_HW.get(ue_id, {}).get("gnb")
                self.add_ue(ue_id, host, user, gnb)

    def set_simulation_mode(self, enabled: bool):
        """Switch between simulation and hardware mode."""
        if enabled != self.simulation_mode:
            self.simulation_mode = enabled
            self._build_collectors()
            logger.info(f"MultiUECollector mode: "
                        f"{'simulation' if enabled else 'hardware'}")

    def add_local_ue(self, ue_id: str, log_file: str = None):
        self.collectors[ue_id] = LocalUECollector(ue_id, log_file)
        logger.info(f"Added local UE collector: {ue_id}")

    def add_ue(self, ue_id: str, hostname: str, ssh_user: str,
               gnb_id: str = None):
        self.collectors[ue_id] = UECollector(ue_id, hostname, ssh_user, gnb_id)
        logger.info(f"Added UE collector: {ue_id} ({ssh_user}@{hostname}, "
                    f"gnb={gnb_id})")

    def remove_ue(self, ue_id: str):
        if ue_id in self.collectors:
            del self.collectors[ue_id]

    def collect_all(self, parallel: bool = True) -> Dict[str, UEMetrics]:
        """Collect metrics from all UEs."""
        results = {}
        if parallel and len(self.collectors) > 1:
            futures = {
                self._executor.submit(collector.collect): ue_id
                for ue_id, collector in self.collectors.items()
            }
            try:
                for future in as_completed(futures, timeout=15):
                    ue_id = futures[future]
                    try:
                        results[ue_id] = future.result()
                    except Exception as e:
                        results[ue_id] = UEMetrics(ue_id=ue_id, error=str(e))
            except TimeoutError:
                for future, ue_id in futures.items():
                    if ue_id not in results:
                        results[ue_id] = UEMetrics(ue_id=ue_id,
                                                   error="collection timeout")
        else:
            for ue_id, collector in self.collectors.items():
                results[ue_id] = collector.collect()
        return results

    def collect_one(self, ue_id: str) -> Optional[UEMetrics]:
        if ue_id not in self.collectors:
            return None
        return self.collectors[ue_id].collect()

    def get_throughput_all(self, duration: float = 2.0) -> Dict[str, float]:
        """Measure throughput for all UEs (parallel, loss-adjusted Mbps).

        Batch F (P0-14): the ACTUAL probe applies the installed ProbeConfig -
        the offered load (iperf3 -b), direction and duration - NOT a hardcoded
        12M. Collectors that don't accept a udp_rate fall back gracefully."""
        cfg = self.probe_config
        udp_rate = self._udp_rate_arg()
        direction = cfg.direction if cfg is not None else "downlink"
        dur = float(cfg.duration_s) if cfg is not None else duration

        # SIGNATURE-AWARE adapter (Batch F review #67db.4): inspect the target's
        # get_throughput signature ONCE and pass ONLY the parameters it accepts,
        # so a udp_rate is never dropped by a blind TypeError-retry (which could
        # silently fall back to an UNLABELLED default probe).
        import inspect

        def _probe(collector):
            fn = collector.get_throughput
            try:
                params = set(inspect.signature(fn).parameters)
            except (TypeError, ValueError):
                params = {"duration", "direction", "udp_rate"}
            kw = {}
            if "duration" in params:
                kw["duration"] = dur
            if "direction" in params:
                kw["direction"] = direction
            if "udp_rate" in params:
                kw["udp_rate"] = udp_rate
            return fn(**kw)

        results = {}
        futures = {
            self._executor.submit(_probe, collector): ue_id
            for ue_id, collector in self.collectors.items()
        }
        try:
            for future in as_completed(futures, timeout=dur + 15):
                ue_id = futures[future]
                try:
                    result = future.result()
                    if result is not None:
                        results[ue_id] = result
                except Exception as e:
                    logger.debug(f"throughput for {ue_id} failed: {e}")
        except TimeoutError:
            logger.warning("throughput collection timed out")
        return results

    def get_goodput_passive_all(self, duration: float = 2.0) -> Dict[str, float]:
        """PASSIVE DL goodput for all UEs in parallel (rx_bytes delta, Mbps).

        The anti-collision KPI path used WHILE the §1-4 background offered load is
        active: no iperf3 injection, no shared iperf3 server, so it can never
        collide with the load (which is what made the active probe read 0.00).
        Uses the installed ProbeConfig's duration/direction (like
        get_throughput_all). A collector without a passive probe is skipped
        gracefully (its UE is simply absent from the result)."""
        import inspect
        cfg = self.probe_config
        dur = float(cfg.duration_s) if cfg is not None else duration
        direction = cfg.direction if cfg is not None else "downlink"

        def _probe(collector):
            fn = getattr(collector, "get_goodput_passive", None)
            if not callable(fn):
                return None
            try:
                params = set(inspect.signature(fn).parameters)
            except (TypeError, ValueError):
                params = {"duration", "direction"}
            kw = {}
            if "duration" in params:
                kw["duration"] = dur
            if "direction" in params:
                kw["direction"] = direction
            return fn(**kw)

        results = {}
        futures = {
            self._executor.submit(_probe, collector): ue_id
            for ue_id, collector in self.collectors.items()
        }
        try:
            for future in as_completed(futures, timeout=dur + 15):
                ue_id = futures[future]
                try:
                    result = future.result()
                    if result is not None:
                        results[ue_id] = result
                except Exception as e:
                    logger.debug(f"passive goodput for {ue_id} failed: {e}")
        except TimeoutError:
            logger.warning("passive goodput collection timed out")
        return results

    def get_aggregate_state(self) -> Dict:
        """Aggregate network state across all UEs."""
        metrics = self.collect_all()

        aggregate = {
            "timestamp": datetime.now().isoformat(),
            "ue_count": len(metrics),
            "attached_count": sum(1 for m in metrics.values() if m.attached),
            "per_ue": {ue_id: m.to_dict() for ue_id, m in metrics.items()}
        }

        throughputs = [m.throughput_mbps for m in metrics.values()
                       if m.throughput_mbps]
        latencies = [m.latency_ms for m in metrics.values() if m.latency_ms]
        rsrps = [m.rsrp for m in metrics.values() if m.rsrp is not None]

        if throughputs:
            aggregate["total_throughput"] = sum(throughputs)
            aggregate["avg_throughput"] = sum(throughputs) / len(throughputs)
            aggregate["min_throughput"] = min(throughputs)
            aggregate["max_throughput"] = max(throughputs)
        if latencies:
            aggregate["avg_latency"] = sum(latencies) / len(latencies)
        if rsrps:
            aggregate["avg_rsrp"] = sum(rsrps) / len(rsrps)

        return aggregate

    def get_ue_ids(self) -> List[str]:
        return list(self.collectors.keys())

    def shutdown(self):
        self._executor.shutdown(wait=False)
