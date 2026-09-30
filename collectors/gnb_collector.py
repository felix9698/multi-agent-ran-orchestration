#!/usr/bin/env python3
"""
OAI gNB Metrics Collector

Collects metrics from OpenAirInterface 5G NR gNB via:
1. Process status (pgrep)
2. Log file parsing
3. Telnet interface queries
"""

import json
import logging
import socket
import subprocess
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("GNBCollector")


@dataclass
class GNBMetrics:
    """OAI gNB metrics"""
    gnb_id: str
    running: bool = False
    connected_ues: int = 0
    pci: int = 0
    cell_id: int = 0

    # RF metrics
    tx_gain: int = 0
    rx_gain: int = 0

    # Resource utilization
    prb_dl_used: int = 0
    prb_ul_used: int = 0
    prb_total: int = 106

    # Performance metrics
    dl_mcs_avg: float = 0.0
    ul_mcs_avg: float = 0.0
    dl_bler: float = 0.0
    ul_bler: float = 0.0

    # UE list
    ue_rntis: List[int] = field(default_factory=list)


class GNBCollector:
    """
    Collects metrics from OAI gNB.

    Uses telnet interface for real-time queries and log parsing
    for historical data.
    """

    def __init__(self, gnb_id: str, hostname: str, ssh_user: str,
                 telnet_ip: str = "127.0.0.1", telnet_port: int = 9090,
                 pci: int = 1, cell_id: int = 1):
        self.gnb_id = gnb_id
        self.hostname = hostname
        self.ssh_user = ssh_user
        self.ssh_target = f"{ssh_user}@{hostname}"
        self.telnet_ip = telnet_ip
        self.telnet_port = telnet_port
        self.pci = pci
        self.cell_id = cell_id
        self.is_local = hostname in ["localhost", "127.0.0.1", socket.gethostname()]

    def _ssh_cmd(self, cmd: str, timeout: float = 5.0) -> Tuple[bool, str]:
        """Execute command via SSH or locally"""
        try:
            if self.is_local:
                result = subprocess.run(
                    ["bash", "-c", cmd],
                    capture_output=True, text=True, timeout=timeout
                )
            else:
                result = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=3", "-o", "StrictHostKeyChecking=no",
                     self.ssh_target, cmd],
                    capture_output=True, text=True, timeout=timeout
                )
            return result.returncode == 0, result.stdout.strip()
        except subprocess.TimeoutExpired:
            return False, "timeout"
        except Exception as e:
            return False, str(e)

    def _telnet_cmd(self, cmd: str, timeout: float = 2.0) -> Tuple[bool, str]:
        """Send command to OAI gNB telnet interface"""
        try:
            if self.is_local:
                # Direct socket connection
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                sock.connect((self.telnet_ip, self.telnet_port))

                # Read banner
                banner = sock.recv(1024).decode()

                # Send command
                sock.send(f"{cmd}\n".encode())

                # Read response
                response = sock.recv(4096).decode()
                sock.close()

                return True, response.strip()
            else:
                # Via SSH tunnel
                telnet_cmd = f"echo '{cmd}' | nc -w 2 {self.telnet_ip} {self.telnet_port}"
                return self._ssh_cmd(telnet_cmd, timeout)

        except Exception as e:
            logger.debug(f"Telnet command failed: {e}")
            return False, str(e)

    def is_running(self) -> bool:
        """Check if OAI gNB process (nr-softmodem) is running"""
        success, output = self._ssh_cmd("pgrep -f 'nr-softmodem'")
        return success and len(output) > 0

    def get_tx_gain(self) -> Optional[int]:
        """Get current TX gain via telnet"""
        success, output = self._telnet_cmd("get_txgain")
        if success:
            match = re.search(r'(\d+)', output)
            if match:
                return int(match.group(1))
        return None

    def get_rx_gain(self) -> Optional[int]:
        """Get current RX gain via telnet"""
        success, output = self._telnet_cmd("get_rxgain")
        if success:
            match = re.search(r'(\d+)', output)
            if match:
                return int(match.group(1))
        return None

    def get_connected_ues(self) -> Tuple[int, List[int]]:
        """Get number of connected UEs and their RNTIs"""
        success, output = self._telnet_cmd("ue_info")
        if not success:
            return 0, []

        ue_count = 0
        rntis = []

        for line in output.split('\n'):
            match = re.search(r'RNTI[=:]\s*(0x[0-9a-fA-F]+|\d+)', line)
            if match:
                rnti_str = match.group(1)
                if rnti_str.startswith('0x'):
                    rntis.append(int(rnti_str, 16))
                else:
                    rntis.append(int(rnti_str))
                ue_count += 1

        return ue_count, rntis

    # ------------------------------------------------------------------ #
    # structured observability (P9): JSON-first via patched "ci stats"    #
    # ------------------------------------------------------------------ #

    def get_ci_stats(self) -> Optional[Dict]:
        """One-line JSON MAC snapshot via the patched `ci stats` command
        (oai_patches/ci_stats_json.patch). None when the gNB does not carry
        the patch / the response is unparseable - callers fall back to the
        legacy stdout-log regex path. RSRP is out of this command's scope
        (PHY/RRC measurement path): log parsing remains its source.
        """
        success, output = self._telnet_cmd("ci stats")
        if not success:
            return None
        return self.parse_ci_stats(output)

    @staticmethod
    def parse_ci_stats(output: str) -> Optional[Dict]:
        """Extract + validate the stats JSON object from a telnet response
        (banner/echo tolerant: scans for the {...} line)."""
        for line in (output or "").splitlines():
            line = line.strip()
            if not (line.startswith("{") and line.endswith("}")):
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            # strict shape check: a wrong-shaped payload must NOT suppress
            # the legacy log-parsing fallback
            if (isinstance(data, dict)
                    and isinstance(data.get("cell"), dict)
                    and isinstance(data.get("ues"), list)):
                return data
        return None

    @staticmethod
    def _fill_from_ci_stats(metrics: "GNBMetrics", stats: Dict):
        """Populate the log-derived fields from a `ci stats` snapshot."""
        ues = [u for u in (stats.get("ues") or []) if isinstance(u, dict)]
        dl = [u["dl_mcs"] for u in ues if isinstance(u.get("dl_mcs"), (int, float))]
        ul = [u["ul_mcs"] for u in ues if isinstance(u.get("ul_mcs"), (int, float))]
        bler = [u["dl_bler"] for u in ues
                if isinstance(u.get("dl_bler"), (int, float))]
        if dl:
            metrics.dl_mcs_avg = sum(dl) / len(dl)
        if ul:
            metrics.ul_mcs_avg = sum(ul) / len(ul)
        if bler:
            metrics.dl_bler = sum(bler) / len(bler)
        rntis = []
        for u in ues:
            r = u.get("rnti")
            try:
                rntis.append(int(r, 16) if isinstance(r, str) else int(r))
            except (TypeError, ValueError):
                continue
        if rntis:
            metrics.ue_rntis = rntis
            metrics.connected_ues = len(rntis)

    def parse_gnb_log(self, log_path: str = "/tmp/gnb.log",
                      lines: int = 100) -> Dict:
        """Parse gNB log for metrics"""
        metrics = {
            "dl_mcs_avg": 0.0,
            "ul_mcs_avg": 0.0,
            "dl_bler": 0.0,
            "ul_bler": 0.0,
            "prb_dl": 0,
            "prb_ul": 0
        }

        cmd = f"tail -{lines} {log_path} 2>/dev/null"
        success, output = self._ssh_cmd(cmd)

        if not success:
            return metrics

        mcs_dl_values = []
        mcs_ul_values = []

        for line in output.split('\n'):
            # Parse DL MCS
            dl_match = re.search(r'DL.*MCS[=:]\s*(\d+)', line, re.I)
            if dl_match:
                mcs_dl_values.append(int(dl_match.group(1)))

            # Parse UL MCS
            ul_match = re.search(r'UL.*MCS[=:]\s*(\d+)', line, re.I)
            if ul_match:
                mcs_ul_values.append(int(ul_match.group(1)))

            # Parse BLER
            bler_match = re.search(r'BLER[=:]\s*([0-9.]+)', line, re.I)
            if bler_match:
                metrics["dl_bler"] = float(bler_match.group(1))

        if mcs_dl_values:
            metrics["dl_mcs_avg"] = sum(mcs_dl_values) / len(mcs_dl_values)
        if mcs_ul_values:
            metrics["ul_mcs_avg"] = sum(mcs_ul_values) / len(mcs_ul_values)

        return metrics

    def collect(self) -> GNBMetrics:
        """Collect all gNB metrics"""
        metrics = GNBMetrics(gnb_id=self.gnb_id, pci=self.pci, cell_id=self.cell_id)

        # Process status
        metrics.running = self.is_running()

        if not metrics.running:
            return metrics

        # RF metrics via telnet
        tx_gain = self.get_tx_gain()
        if tx_gain is not None:
            metrics.tx_gain = tx_gain

        rx_gain = self.get_rx_gain()
        if rx_gain is not None:
            metrics.rx_gain = rx_gain

        # Connected UEs
        ue_count, rntis = self.get_connected_ues()
        metrics.connected_ues = ue_count
        metrics.ue_rntis = rntis

        # Link stats: JSON-first via the patched `ci stats` (P9), falling
        # back to the legacy stdout-log regex parsing on unpatched gNBs
        stats = self.get_ci_stats()
        if stats is not None:
            self._fill_from_ci_stats(metrics, stats)
        else:
            log_metrics = self.parse_gnb_log()
            metrics.dl_mcs_avg = log_metrics.get("dl_mcs_avg", 0.0)
            metrics.ul_mcs_avg = log_metrics.get("ul_mcs_avg", 0.0)
            metrics.dl_bler = log_metrics.get("dl_bler", 0.0)
            metrics.ul_bler = log_metrics.get("ul_bler", 0.0)

        return metrics


class MultiGNBCollector:
    """Collects metrics from multiple gNBs"""

    def __init__(self, gnb_configs: Dict[str, Dict] = None):
        """
        Args:
            gnb_configs: gNB configurations from config.py
        """
        self.collectors: Dict[str, GNBCollector] = {}

        if gnb_configs:
            for gnb_id, config in gnb_configs.items():
                self.collectors[gnb_id] = GNBCollector(
                    gnb_id=gnb_id,
                    hostname=config.get("hostname", "localhost"),
                    ssh_user=config.get("ssh_user", "root"),
                    telnet_ip=config.get("telnet_ip", "127.0.0.1"),
                    telnet_port=config.get("telnet_port", 9090),
                    pci=config.get("pci", 1),
                    cell_id=config.get("cell_id", 1)
                )

    def add_gnb(self, gnb_id: str, **kwargs):
        """Add a gNB collector"""
        self.collectors[gnb_id] = GNBCollector(gnb_id=gnb_id, **kwargs)

    def collect_all(self) -> Dict[str, GNBMetrics]:
        """Collect metrics from all gNBs"""
        return {gnb_id: collector.collect()
                for gnb_id, collector in self.collectors.items()}

    def get_running_gnbs(self) -> List[str]:
        """Get list of running gNB IDs"""
        return [gnb_id for gnb_id, collector in self.collectors.items()
                if collector.is_running()]
