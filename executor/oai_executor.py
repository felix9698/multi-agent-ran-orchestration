#!/usr/bin/env python3
"""
OAI gNB Executor

Runtime control of OAI nr-softmodem via its telnet server (--telnetsrv).

The stock OAI telnet server has NO runtime scheduling commands; this executor
relies on the project's patched "ci" module (common/utils/telnetsrv/telnetsrv_ci.c):

    ci rfatt            -> print current TX attenuation (dB below max gain)
    ci rfatt <att_dB>   -> apply new TX attenuation immediately (no restart)
    ci trigger_n2_ho <neighbour_pci> <ueId>  -> stock N2 handover trigger

The d>=2 action-space expansion (Extension Strategy) adds THREE more runtime
knobs. `ci rfatt` already ships on the testbed; the three below are designed in
oai_patches/ and require a one-time gNB rebuild (Phase 2 -- see
docs/action_space_d2.md). Their in-memory targets in the OAI MAC (RC.nrmac[0])
are re-read every scheduling slot, so a write takes effect on the next slot with
no restart, exactly like rfatt:

    ci mcs [max [min]]        -> DL MCS cap  (RC.nrmac[0]->dl_bler.max_mcs/min_mcs)
    ci prbcap [n_prb [rnti]]  -> DL PRB cap  (cell: RC.nrmac[0]->dl_max_rbSize;
                                              per-UE: sched_ctrl->dl_max_rbSize; 0=uncapped)
    ci sched_prio [w [rnti]]  -> PF weight   (per-UE sched_ctrl->pf_weight; 1.0=neutral)

Two axes (PRB cap and scheduling priority) can be addressed either per-cell
(no rnti; applies to the whole gNB) or per-UE (rnti given; applies to one UE),
so an intra-cell fairness intent can partition resources between UEs that share
a cell (the paper's UE1/UE2 -> BS1 topology). Power and MCS remain cell-wide.

Per-axis conventions used by the coordinator:

  * Power offset (matches the srsRAN "cell_gain" semantics of the ICC paper):
        offset_db = BASE_ATT_DB - current_att_tx
        offset +3 dB  -> att_tx 9  (more TX power)
        offset -5 dB  -> att_tx 17 (less TX power)
    BASE_ATT_DB must equal att_tx in the gNB .conf files (12 dB).

  * MCS offset (relative to a base cap, symmetric-around-0 like power):
        effective DL MCS cap = BASE_MCS_CAP + offset  (clamped to [0, BASE_MCS_CAP])
        offset 0   -> cap 28 == unconstrained link adaptation (today's behavior)
        offset -18 -> cap 10 (conservative link / degradation lever)

  * PRB allocation: an ABSOLUTE per-slot DL PRB cap. 0 == uncapped (full carrier,
    today's behavior); a positive value caps each UE's DL grant.

  * Scheduling priority: a proportional-fair weight multiplier per gNB's UE.
        1.0 == neutral PF (today's behavior); >1 favors, <1 starves.

The gNB must be launched with:
    --telnetsrv --telnetsrv.shrmod ci --telnetsrv.listenport 9091
(port 9091 because leftover Open5GS services occupy 9090 on PC1 loopback,
and a telnet bind failure kills nr-softmodem).
"""

import contextlib
import logging
import re
import socket
import time
import threading
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, Optional, Tuple

logger = logging.getLogger("OAIExecutor")

# Reusable no-op context manager: stands in for the reachability lock on
# __new__-built test skeletons that skipped __init__ (so _telnet_cmd's backoff
# section never needs to branch on whether the lock exists).
_NULLCTX = contextlib.nullcontext()

# Baseline TX attenuation configured in all gNB conf files (= 0 dB offset)
BASE_ATT_DB = 12.0
# Hardware safety bounds for attenuation
ATT_MIN_DB = 0.0
ATT_MAX_DB = 30.0
TELNET_PORT = 9091
# After a telnet CONNECT failure to a host, suppress further connect attempts to
# that host for this many seconds (per-host, auto-recovering). Stops a gNB that
# is absent from the current bring-up (e.g. gnb2 on a single-gNB testbed) from
# spamming "[Errno 111] Connection refused" on every call and adding a connect
# stall to each control cycle. Reachability resumes automatically after the
# window (a successful connect clears the backoff immediately).
TELNET_BACKOFF_S = 5.0

# --- d>=2 action-space hardware/protocol safety bounds ---------------------- #
# These are the executor's own defense-in-depth clamps (the coordinator also
# clips to the configured action space U before calling in). They mirror the
# ATT_MIN/MAX_DB pattern of the power axis.
#
# MCS: OAI MCS table 1 tops out at 27, table 2 at 28; dl_max_mcs default = 28.
BASE_MCS_CAP = 28               # offset 0 -> this cap == unconstrained (today)
MCS_CAP_MIN = 0                 # cap cannot go below MCS index 0
MCS_CAP_MAX = 28
# PRB: a positive cap must be a usable grant; 0 is the "uncapped" sentinel.
PRB_CAP_MIN = 1
PRB_CAP_MAX = 273               # largest NR FR1 carrier (100 MHz / 30 kHz SCS)
PRB_UNCAPPED = 0
# PF scheduling weight multiplier.
PRIO_MIN = 0.05
PRIO_MAX = 20.0
PRIO_NEUTRAL = 1.0

# P7 read-back sentinel: the axis has no observable device object in the
# current topology (e.g. cell-level PF weight on a multi-UE cell) - distinct
# from None, which means the read itself failed.
UNOBSERVABLE = object()


@dataclass
class GNBControlState:
    """Tracks the last applied control state of one gNB (all d>=2 axes).

    The scalar axes (tx_att_db/prb_cap/sched_priority/mcs_cap) are the CELL-level
    knobs. Two axes can additionally be addressed per UE (per RNTI) for
    intra-cell fairness whenever a cell is shared (current testbed: UE1+UE2 on
    gNB1; the UE count is variable, not a fixed 3-UE scenario); those overrides
    live in the ue_* dicts (keyed by RNTI), with an absent RNTI meaning "neutral".
    """
    gnb_id: str
    host: str
    pci: int
    tx_att_db: float = BASE_ATT_DB      # axis 1: TX power (attenuation dB)
    prb_cap: int = PRB_UNCAPPED         # axis 2: DL PRB cap (0 = uncapped), cell-wide
    sched_priority: float = PRIO_NEUTRAL  # axis 3: PF weight (1.0 = neutral)
    mcs_cap: int = BASE_MCS_CAP         # axis 4: DL MCS cap (BASE = uncapped)
    # Per-UE (per-RNTI) overrides for the two intra-cell fairness axes. Absent
    # RNTI == neutral (PF weight 1.0 / PRB uncapped).
    ue_sched_priority: Dict[int, float] = field(default_factory=dict)
    ue_prb_cap: Dict[int, int] = field(default_factory=dict)
    # P8: last observed RRC re-establishment counter (None = never read).
    # A change since the last observation means RNTIs may be stale.
    reestab_count: Optional[int] = None
    last_update: float = 0.0

    @property
    def power_offset_db(self) -> float:
        return BASE_ATT_DB - self.tx_att_db

    @property
    def mcs_offset(self) -> float:
        """MCS ceiling relative to the base cap (0 = unconstrained)."""
        return float(self.mcs_cap - BASE_MCS_CAP)

    def axis_value(self, axis: str, rnti: Optional[int] = None):
        """Current value of one action axis (in the coordinator's units).

        With `rnti` set, returns the per-UE override for the PRB / scheduling
        axes (falling back to the axis neutral if the UE has no override).
        """
        if rnti is not None:
            if axis == "sched_priority":
                return self.ue_sched_priority.get(rnti, PRIO_NEUTRAL)
            if axis == "prb":
                return self.ue_prb_cap.get(rnti, PRB_UNCAPPED)
            raise KeyError(f"axis {axis!r} is not per-UE addressable")
        return {
            "power_offset": self.power_offset_db,
            "prb": self.prb_cap,
            "sched_priority": self.sched_priority,
            "mcs_offset": self.mcs_offset,
        }[axis]

    def as_snapshot(self) -> Dict[str, float]:
        """Full multi-axis snapshot of this gNB (for deterministic rollback).

        Includes the per-UE override dicts (copied) so a trial that biased a
        single UE is rolled back per-RNTI, not just at the cell level.
        """
        return {
            "power_offset_db": self.power_offset_db,
            "prb_cap": self.prb_cap,
            "sched_priority": self.sched_priority,
            "mcs_offset": self.mcs_offset,
            "ue_sched_priority": dict(self.ue_sched_priority),
            "ue_prb_cap": dict(self.ue_prb_cap),
        }


class OAIExecutor:
    """
    Executes runtime control commands on OAI gNBs via telnet.

    Both gNBs are reached over plain TCP (the OAI telnet server binds
    0.0.0.0), so no SSH tunnelling is needed:
        gnb1 -> 127.0.0.1:9091 (local on PC1)
        gnb2 -> 192.168.0.51:9091 (PC2 over LAN)
    """

    DEFAULT_GNBS = {
        "gnb1": {"host": "127.0.0.1", "pci": 0},
        "gnb2": {"host": "192.168.0.51", "pci": 1},
    }

    def __init__(self, gnb_configs: Optional[Dict[str, Dict]] = None,
                 telnet_port: int = TELNET_PORT):
        self.telnet_port = telnet_port
        cfgs = gnb_configs or self.DEFAULT_GNBS
        self.states: Dict[str, GNBControlState] = {
            gnb_id: GNBControlState(
                gnb_id=gnb_id,
                host=cfg.get("host", "127.0.0.1"),
                pci=cfg.get("pci", 0),
            )
            for gnb_id, cfg in cfgs.items()
        }
        self._lock = Lock()
        # P7 verified actuation: full post-restore readback report
        # {gnb: {axis | "axis@rnti": (snapshot, readback, ok)}}
        self.last_restore_report: Dict[str, Dict] = {}
        # P8 RNTI integrity guard: optional registry-based resolver
        # (gnb_id, stale_rnti) -> new_rnti|None (the coordinator wires its
        # UE->RNTI registration map here), a remap notification callback
        # (gnb_id, old, new), and an event log of guard decisions/reasons.
        self.rnti_resolver = None
        self.on_rnti_remap = None
        self.last_rnti_guard_events: list = []
        # Batch B (P0-1 step 9): an INDEPENDENT executor-side safety latch.
        # Once engaged, every ordinary write API (apply_axis, the set_*
        # primitives, trigger_handover) is refused, so a caller reaching the
        # executor directly - outside the coordinator object - still cannot
        # actuate.  The ONLY writes allowed while latched are those issued from
        # inside restore() (the recovery path).  The bypass is THREAD-SCOPED
        # (item 6): restore() authorizes ONLY its own calling thread via a
        # thread-local token, so a concurrent thread can never write while
        # latched just because some other thread is restoring.
        self._latched = False
        self._latch_reason = None
        self._restore_local = threading.local()
        # Per-host telnet unreachability backoff: host -> monotonic deadline
        # until which connect attempts are skipped (Task 2). Guarded by its own
        # lock so it can be touched from inside a self._lock write section
        # without deadlock (never acquired in the other order).
        self.telnet_backoff_s = TELNET_BACKOFF_S
        self._unreachable: Dict[str, float] = {}
        self._reach_lock = Lock()
        logger.info(f"OAIExecutor initialized: "
                    f"{[(s.gnb_id, s.host, s.pci) for s in self.states.values()]}")

    # ------------------------------------------------------------------ #
    # safety latch (P0-1 step 9): fail-closed direct-write protection      #
    # ------------------------------------------------------------------ #

    @property
    def is_latched(self) -> bool:
        return bool(getattr(self, "_latched", False))

    def latch_failsafe(self, reason: str = "") -> None:
        """Engage the executor latch; ordinary writes are refused afterwards."""
        self._latched = True
        self._latch_reason = str(reason)
        logger.error(f"executor latched (TechnicalFailsafe): {reason}")

    def _clear_latch(self) -> None:
        """Release the executor latch. PRIVATE: the ONLY caller is
        recover_and_clear(), AFTER a full-baseline readback verification. There
        is deliberately NO public unconditional clear (item 5) - an operator
        must go through recover_and_clear so a latch can never be dropped
        without proving the device matches the recovery target."""
        self._latched = False
        self._latch_reason = None
        logger.info("executor latch cleared (post full-readback verification)")

    def _restore_authorized(self) -> bool:
        """True iff THIS thread is inside a restore/recovery call (item 6:
        thread-scoped, not a global flag)."""
        return bool(getattr(self._restore_local, "active", False)) \
            if hasattr(self, "_restore_local") else False

    def _write_blocked(self) -> bool:
        """True iff an ordinary write must be refused: latched AND the CURRENT
        thread is not inside the restore/recovery path.  getattr fallbacks keep
        __new__-built test skeletons (which skip __init__) writable by default."""
        return (getattr(self, "_latched", False)
                and not self._restore_authorized())

    def recover_and_clear(self, target_snapshot: Dict) -> bool:
        """Operator recovery: re-apply the recovery/baseline target and clear
        the latch ONLY when a FULL device read-back of every applicable target
        axis verifies (item 5).

        Unlike the diff-based restore audit (which only re-reads TOUCHED axes,
        so a stale mirror could false-clear), this re-reads EVERY
        device-readable target axis fresh and compares it to the target. The
        latch is cleared IFF the full verification passes; otherwise it stays
        engaged. Returns True iff the latch was actually cleared."""
        # restore() sets this thread's recovery authorization, so the re-apply
        # is permitted even while latched.
        self.restore(target_snapshot)
        if self._verify_full_snapshot(target_snapshot):
            self._clear_latch()
            return True
        logger.error("recover_and_clear: full read-back did NOT verify - "
                     "latch stays engaged")
        return False

    def _verify_full_snapshot(self, snapshot: Dict) -> bool:
        """Full-baseline verification: read back EVERY device-readable target
        axis (per its snapshot_source provenance) and compare to the target -
        NOT only the axes a diff-based restore happened to touch.

        Per-UE axes in the target are verified too. Axes whose baseline
        provenance is not an explicit device read (mirror/legacy) cannot be
        confirmed on this gNB and are not required (a stock power-only gNB
        cannot report prb/sched/mcs); but if NO axis is verifiable the result
        is False (fail closed)."""
        verified_any = False
        for gnb_id, snap in (snapshot or {}).items():
            state = self.states.get(gnb_id)
            if state is None:
                return False
            if not isinstance(snap, dict):
                snap = {"power_offset_db": float(snap)}
            source = snap.get("snapshot_source") or {}
            targets = self._snapshot_targets(snap)
            for axis, target in targets.items():
                if target is None:
                    continue
                if source and source.get(axis) != "device":
                    continue          # not device-readable on this gNB
                reading = self._read_axis(state, axis)
                if reading is None or abs(float(reading) - float(target)) \
                        > self._AXIS_EPS[axis]:
                    logger.error(f"full verify: {gnb_id} {axis} device="
                                 f"{reading} target={target}")
                    return False
                verified_any = True
            # per-UE target axes
            for axis, snap_key in (("sched_priority", "ue_sched_priority"),
                                   ("prb", "ue_prb_cap")):
                key = "ue_sched" if axis == "sched_priority" else "ue_prb"
                if source and source.get(key) != "device":
                    continue
                for rnti, target in (snap.get(snap_key) or {}).items():
                    reading = self._read_axis(state, axis, int(rnti))
                    if reading is None or abs(float(reading) - float(target)) \
                            > self._AXIS_EPS[axis]:
                        logger.error(f"full verify: {gnb_id} {axis}@{rnti} "
                                     f"device={reading} target={target}")
                        return False
                    verified_any = True
        return verified_any

    # ------------------------------------------------------------------ #
    # low-level telnet transport (per-command connection, stateless)      #
    # ------------------------------------------------------------------ #

    def _telnet_cmd(self, host: str, cmd: str,
                    timeout: float = 3.0) -> Tuple[bool, str]:
        """Send one command to the OAI telnet shell and return its output.

        A fresh connection per command: the OAI telnet server serves one
        client at a time, so holding connections open would starve other
        tools (and reconnect handling is simpler than keep-alive probing).

        A host that fails to CONNECT is put in a short per-host backoff
        (self.telnet_backoff_s): subsequent commands to it short-circuit
        without a connect attempt (no per-call log spam, no connect stall)
        until the window expires, then one probe is retried. A successful
        connect clears the backoff immediately, so a gNB coming online
        recovers on its own. Post-connect read errors are NOT backed off
        (the host is present) - they are logged and returned per occurrence.
        """
        # getattr fallbacks keep __new__-built test skeletons (which skip
        # __init__) working with sane defaults.
        backoff = getattr(self, "telnet_backoff_s", TELNET_BACKOFF_S)
        unreach = getattr(self, "_unreachable", None)
        if unreach is None:
            unreach = self._unreachable = {}
        rlock = getattr(self, "_reach_lock", None)

        now = time.monotonic()
        with (rlock or _NULLCTX):
            deadline = unreach.get(host)
        if deadline is not None and now < deadline:
            # Known-unreachable within the window: skip the connect entirely.
            return False, "unreachable (telnet backoff active)"

        try:
            sock = socket.create_connection((host, self.telnet_port),
                                            timeout=timeout)
        except OSError as e:
            with (rlock or _NULLCTX):
                first = host not in unreach
                unreach[host] = now + backoff
            if first:
                logger.warning(
                    f"telnet {host}:{self.telnet_port} '{cmd}' failed: {e} - "
                    f"suppressing further attempts for {backoff:g}s")
            return False, str(e)

        # Connected: a live host clears any prior backoff (auto-recovery).
        if deadline is not None:
            with (rlock or _NULLCTX):
                unreach.pop(host, None)
            logger.info(f"telnet {host}:{self.telnet_port} reachable again")
        try:
            with sock:
                sock.settimeout(timeout)
                self._read_until_prompt(sock)          # banner + first prompt
                sock.sendall((cmd + "\n").encode())
                response = self._read_until_prompt(sock)
                return True, self._clean_response(cmd, response)
        except OSError as e:
            logger.warning(f"telnet {host}:{self.telnet_port} '{cmd}' "
                           f"read failed: {e}")
            return False, str(e)

    @staticmethod
    def _clean_response(cmd: str, response: str) -> str:
        """Strip transport framing from a telnet response: the echoed command
        line and the TRAILING OAI prompt ("..._gnb> "). The prompt must not
        survive into the output - a command whose real output is EMPTY (e.g.
        the patched argless "ci sched_prio" with zero UEs) has to come back
        as an empty string, not as the prompt text (C6: it would otherwise
        be misclassified as an unrecognized response). Only the LAST line is
        prompt-checked, and only a single-token line ending in '>' counts:
        legitimate output lines may end with '>' too (usage text such as
        "ci prbcap <n> <rnti>").
        """
        lines = [ln for ln in response.splitlines()
                 if ln.strip() and not ln.strip().startswith(cmd)]
        if lines and re.fullmatch(r"\S*>\s*", lines[-1]):
            lines.pop()
        return "\n".join(lines).strip()

    @staticmethod
    def _read_until_prompt(sock: socket.socket, max_bytes: int = 65536) -> str:
        """Read until the OAI prompt ('..._gnb> ') or timeout."""
        data = b""
        while len(data) < max_bytes:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
            if data.rstrip(b" ").endswith(b">") or data.endswith(b"> "):
                break
        return data.decode(errors="replace")

    # ------------------------------------------------------------------ #
    # TX power control                                                    #
    # ------------------------------------------------------------------ #

    def set_tx_att(self, gnb_id: str, att_db: float) -> bool:
        """Set absolute TX attenuation (dB below max gain) on one gNB."""
        if self._write_blocked():
            logger.error(f"{gnb_id}: rfatt refused - executor latched")
            return False
        state = self.states.get(gnb_id)
        if not state:
            logger.error(f"Unknown gNB: {gnb_id}")
            return False

        # quantize to the wire format (%.1f) so mirror == device exactly
        att_db = round(max(ATT_MIN_DB, min(ATT_MAX_DB, float(att_db))), 1)

        with self._lock:
            success, output = self._telnet_cmd(state.host, f"ci rfatt {att_db:.1f}")
            if success and "set to" in output:
                state.tx_att_db = att_db
                state.last_update = time.time()
                logger.info(f"{gnb_id}: TX att = {att_db:.1f} dB "
                            f"(offset {state.power_offset_db:+.1f} dB)")
                return True
            logger.error(f"{gnb_id}: rfatt failed: {output}")
            return False

    def set_power_offset(self, gnb_id: str, offset_db: float) -> bool:
        """Set TX power offset relative to the 0 dB baseline (BASE_ATT_DB)."""
        return self.set_tx_att(gnb_id, BASE_ATT_DB - float(offset_db))

    def get_power_offset(self, gnb_id: str) -> Optional[float]:
        """Read back the offset from the gNB (falls back to cached state)."""
        state = self.states.get(gnb_id)
        if not state:
            return None
        success, output = self._telnet_cmd(state.host, "ci rfatt")
        if success:
            m = re.search(r"attenuation\s+([-\d.]+)", output)
            if m:
                state.tx_att_db = float(m.group(1))
                return state.power_offset_db
        return state.power_offset_db

    def get_all_offsets(self) -> Dict[str, float]:
        """Cached offsets for all gNBs (no network round trip)."""
        return {gid: s.power_offset_db for gid, s in self.states.items()}

    # ------------------------------------------------------------------ #
    # d>=2 runtime knobs: PRB cap, scheduling priority, MCS cap           #
    # (patched telnet "ci prbcap" / "ci sched_prio" / "ci mcs"; the gNB   #
    #  must be rebuilt with oai_patches/ before these succeed -- Phase 2) #
    # ------------------------------------------------------------------ #

    def set_prb_allocation(self, gnb_id: str, n_prb,
                           rnti: Optional[int] = None) -> bool:
        """Cap the DL PRB grant on one gNB (0 = uncapped/full carrier).

        Runtime knob. With `rnti` None the CELL cap is written
        (RC.nrmac[0]->dl_max_rbSize, applied to every UE on the cell). With an
        `rnti`, the PER-UE cap is written (that UE's sched_ctrl->dl_max_rbSize);
        the scheduler applies the tighter of the two. Both are re-read by the DL
        scheduler every slot. `n_prb` <= 0 removes the cap. The per-UE form needs
        the extended "ci prbcap <n> <rnti>" telnet command (oai_patches/).
        """
        state = self.states.get(gnb_id)
        if not state:
            logger.error(f"Unknown gNB: {gnb_id}")
            return False

        if self._write_blocked():
            logger.error(f"{gnb_id}: prbcap refused - executor latched")
            return False

        n_prb = int(round(float(n_prb)))
        if n_prb <= 0:
            n_prb = PRB_UNCAPPED
        else:
            n_prb = max(PRB_CAP_MIN, min(PRB_CAP_MAX, n_prb))

        cmd = f"ci prbcap {n_prb}"
        if rnti is not None:
            cmd += f" {int(rnti):x}"

        with self._lock:
            success, output = self._telnet_cmd(state.host, cmd)
            if success and "set to" in output:
                cap_txt = "uncapped" if n_prb == PRB_UNCAPPED else f"{n_prb} PRB"
                if rnti is not None:
                    state.ue_prb_cap[int(rnti)] = n_prb
                    logger.info(f"{gnb_id}: UE {int(rnti):04x} DL PRB cap = {cap_txt}")
                else:
                    state.prb_cap = n_prb
                    logger.info(f"{gnb_id}: DL PRB cap = {cap_txt}")
                state.last_update = time.time()
                return True
            logger.error(f"{gnb_id}: prbcap failed: {output}")
            return False

    def set_sched_priority(self, gnb_id: str, weight, rnti: Optional[int] = None) -> bool:
        """Set the proportional-fair scheduling weight for a gNB's UE(s).

        Runtime knob: writes a UE's sched_ctrl->pf_weight (the PF coefficient is
        multiplied by it), re-read every slot. weight 1.0 = neutral PF; >1
        favors, <1 starves.

        * With an `rnti` a specific UE is addressed (`ci sched_prio <w> <rnti>`)
          -- required for intra-cell fairness when UEs share a cell. Unchanged.
        * With `rnti` None (a CELL-level intent, e.g. an LLM `bsN_sched_priority`
          proposal) the canonical argless `ci sched_prio <w>` is tried first. On
          a single-UE cell the patched gNB applies it directly (unchanged, wire-
          identical to before). But the paper topology puts UE1+UE2 on gNB1, and
          the patched gNB then FAILS CLOSED ("could not identify UE ... multiple
          UEs") because there is no cell-wide PF-weight object -- OAI keeps one
          scheduler weight PER UE. So when the argless write is refused and >1 UE
          shares the cell, the same weight is FANNED OUT to every connected UE
          (the only meaningful per-cell interpretation). The fan-out is ATOMIC:
          if any per-UE write fails, the already-applied UEs are rolled back to
          their pre-call weights and False is returned. Fan-out targets live only
          in the per-UE mirror (`ue_sched_priority`), so snapshot/restore roll
          them back per-RNTI through the existing per-UE path.
        """
        state = self.states.get(gnb_id)
        if not state:
            logger.error(f"Unknown gNB: {gnb_id}")
            return False

        if self._write_blocked():
            logger.error(f"{gnb_id}: sched_prio refused - executor latched")
            return False

        # quantize to the wire format (%.3f) so mirror == device exactly
        weight = round(max(PRIO_MIN, min(PRIO_MAX, float(weight))), 3)

        if rnti is not None:
            return self._sched_prio_write_one(state, gnb_id, weight, int(rnti))

        # Cell-level intent: try the canonical argless write first so the single-
        # UE / stock-gNB path stays byte-identical to the hardware-verified one.
        with self._lock:
            success, output = self._telnet_cmd(state.host,
                                               f"ci sched_prio {weight:.3f}")
            if success and "set to" in output:
                state.sched_priority = weight
                state.last_update = time.time()
                logger.info(f"{gnb_id}: PF weight = {weight:.3f}")
                return True
        if not success:
            # transport failure (telnet down): do NOT fan out - it would fail too
            logger.error(f"{gnb_id}: sched_prio failed: {output}")
            return False
        # Transport OK but the gNB refused the cell-wide write. If >1 UE shares
        # the cell, that refusal is expected (no cell-wide weight object) -> fan
        # the weight out to each connected UE. Any other refusal (0 UEs, unknown
        # command) stays a failure.
        rntis = self._list_connected_rntis(state)
        if rntis and len(rntis) > 1:
            return self._sched_prio_fanout(state, gnb_id, weight, rntis)
        logger.error(f"{gnb_id}: sched_prio failed: {output}")
        return False

    def _sched_prio_write_one(self, state: GNBControlState, gnb_id: str,
                              weight: float, rnti: int) -> bool:
        """Write ONE UE's PF weight (`ci sched_prio <w> <rnti>`) and mirror it.

        The single per-UE primitive shared by the direct rnti path and the
        cell fan-out. Holds the executor lock for the telnet round trip only;
        callers must NOT hold the lock (it is not re-entrant)."""
        with self._lock:
            success, output = self._telnet_cmd(
                state.host, f"ci sched_prio {weight:.3f} {int(rnti):x}")
            if success and "set to" in output:
                state.ue_sched_priority[int(rnti)] = weight
                state.last_update = time.time()
                logger.info(f"{gnb_id}: UE {int(rnti):04x} PF weight = {weight:.3f}")
                return True
            logger.error(f"{gnb_id}: sched_prio failed (UE {int(rnti):04x}): "
                         f"{output}")
            return False

    def _list_connected_rntis(self, state: GNBControlState):
        """All connected UE RNTIs on the cell, from the patched argless
        `ci sched_prio` ("UE <rnti> PF weight <w>", one row per connected UE).

        Returns a list of ints; [] when the patched gNB reports zero UEs;
        None when the read fails or the output is not the patched format (so
        callers can fall back safely). Does NOT change any wire command."""
        ok, out = self._telnet_cmd(state.host, "ci sched_prio")
        if not ok:
            return None
        stripped = out.strip()
        if not stripped or "no UEs" in stripped:
            return []                       # patched gNB, zero UEs connected
        rntis = [int(x, 16)
                 for x in re.findall(r"UE ([0-9a-fA-F]+) PF weight", out)]
        return rntis if rntis else None     # non-empty but unrecognized -> None

    def _sched_prio_fanout(self, state: GNBControlState, gnb_id: str,
                           weight: float, rntis) -> bool:
        """Apply one PF weight to EVERY connected UE (cell-wide intent on a
        shared cell). Atomic: on any per-UE failure, the UEs already changed in
        THIS call are restored to their pre-call weights and False is returned,
        so a partial fan-out never leaks into the mirror or the device."""
        # Snapshot the pre-call per-UE weights so a partial failure is undone.
        previous = {int(r): state.ue_sched_priority.get(int(r), PRIO_NEUTRAL)
                    for r in rntis}
        applied = []
        for r in rntis:
            r = int(r)
            if self._sched_prio_write_one(state, gnb_id, weight, r):
                applied.append(r)
                continue
            logger.error(f"{gnb_id}: sched_prio fan-out failed on UE {r:04x}; "
                         f"rolling back {len(applied)} already-applied UE(s)")
            for ar in applied:
                if not self._sched_prio_write_one(state, gnb_id, previous[ar], ar):
                    logger.error(f"{gnb_id}: fan-out rollback FAILED on UE "
                                 f"{ar:04x} (device may be inconsistent - the "
                                 f"caller's snapshot restore is the backstop)")
            return False
        logger.info(f"{gnb_id}: PF weight {weight:.3f} fanned out to "
                    f"{len(applied)} UE(s): "
                    f"{[format(r, '04x') for r in applied]}")
        return True

    def set_mcs_cap(self, gnb_id: str, max_mcs: int) -> bool:
        """Set the absolute DL MCS cap on one gNB (RC.nrmac[0]->dl_bler.max_mcs).

        Runtime knob, re-read every slot (unlike the per-UE sched_ctrl->dl_max_mcs,
        which CSI overwrites). BASE_MCS_CAP == unconstrained link adaptation.
        """
        state = self.states.get(gnb_id)
        if not state:
            logger.error(f"Unknown gNB: {gnb_id}")
            return False

        if self._write_blocked():
            logger.error(f"{gnb_id}: mcs refused - executor latched")
            return False

        max_mcs = int(max(MCS_CAP_MIN, min(MCS_CAP_MAX, int(round(max_mcs)))))
        with self._lock:
            success, output = self._telnet_cmd(state.host, f"ci mcs {max_mcs}")
            if success and "set to" in output:
                state.mcs_cap = max_mcs
                state.last_update = time.time()
                logger.info(f"{gnb_id}: DL MCS cap = {max_mcs} "
                            f"(offset {state.mcs_offset:+.0f})")
                return True
            logger.error(f"{gnb_id}: mcs failed: {output}")
            return False

    def set_mcs_offset(self, gnb_id: str, offset) -> bool:
        """Lower/raise the DL MCS ceiling by `offset` relative to the base cap.

        effective cap = BASE_MCS_CAP + offset (clamped to [MCS_CAP_MIN, MCS_CAP_MAX]).
        offset 0 = unconstrained (today). Symmetric-around-0 knob for the LLM,
        mirroring the power-offset convention.
        """
        cap = int(round(BASE_MCS_CAP + float(offset)))
        return self.set_mcs_cap(gnb_id, cap)

    def get_prb_allocation(self, gnb_id: str,
                           rnti: Optional[int] = None) -> Optional[int]:
        """Cached DL PRB cap for one gNB (0 = uncapped); per-UE with `rnti`."""
        state = self.states.get(gnb_id)
        if not state:
            return None
        if rnti is not None:
            return state.ue_prb_cap.get(int(rnti), PRB_UNCAPPED)
        return state.prb_cap

    def get_sched_priority(self, gnb_id: str,
                           rnti: Optional[int] = None) -> Optional[float]:
        """Cached PF weight for one gNB's UE (cell-level, or per-UE with `rnti`)."""
        state = self.states.get(gnb_id)
        if not state:
            return None
        if rnti is not None:
            return state.ue_sched_priority.get(int(rnti), PRIO_NEUTRAL)
        return state.sched_priority

    def get_mcs_offset(self, gnb_id: str) -> Optional[float]:
        """Cached DL MCS offset for one gNB (0 = uncapped)."""
        state = self.states.get(gnb_id)
        return state.mcs_offset if state else None

    def apply_axis(self, gnb_id: str, axis: str, value,
                   rnti: Optional[int] = None, verify: bool = True) -> bool:
        """Dispatch one action axis to its runtime primitive.

        axis in {power_offset, prb, sched_priority, mcs_offset}. This is the
        single entry point the coordinator uses so trial application and
        rollback share exactly one code path per axis. `rnti` selects per-UE
        addressing and is only valid for the two per-UE axes (prb,
        sched_priority); power/MCS are cell-wide and reject a per-UE target.

        With verify=True (default, P7 verified actuation) the write is
        followed by a device READ-BACK compared against the mirror: a gNB
        that acknowledged but did not actually apply returns False, taking
        the existing safe path (apply failure -> snapshot restore ->
        negotiation).
        """
        # Batch B (P0-1): direct-write latch. apply_axis is the coordinator's
        # single write entry point AND is reachable directly, so the latch is
        # enforced here too - a latched executor refuses every ordinary write.
        # restore() sets _restoring, so its own apply_axis calls are permitted.
        if self._write_blocked():
            logger.error(f"{gnb_id}: apply_axis {axis!r} refused - "
                         f"executor latched")
            return False
        if rnti is not None:
            if axis not in ("prb", "sched_priority"):
                logger.error(f"apply_axis: axis {axis!r} is not per-UE addressable")
                return False
            # P8 RNTI integrity guard: an RRC re-establishment since the
            # last observation means this RNTI may be stale
            rnti = self._guard_rnti(gnb_id, int(rnti))
            if rnti is None:
                return False
            if axis == "prb":
                ok = self.set_prb_allocation(gnb_id, value, rnti=rnti)
            else:
                ok = self.set_sched_priority(gnb_id, value, rnti=rnti)
        elif axis == "power_offset":
            ok = self.set_power_offset(gnb_id, value)
        elif axis == "prb":
            ok = self.set_prb_allocation(gnb_id, value)
        elif axis == "sched_priority":
            ok = self.set_sched_priority(gnb_id, value)
        elif axis == "mcs_offset":
            ok = self.set_mcs_offset(gnb_id, value)
        else:
            logger.error(f"apply_axis: unknown axis {axis!r}")
            return False
        if not ok:
            return False
        if not verify:
            return True
        # compare the device against the MIRROR (post-clamp/quantization)
        expected = self.get_axis(gnb_id, axis, rnti)
        return self.verify_axis(gnb_id, axis, expected, rnti=rnti)

    # ------------------------------------------------------------------ #
    # verified actuation (P7): write-then-read-back                       #
    # ------------------------------------------------------------------ #

    def _read_axis(self, state: GNBControlState, axis: str,
                   rnti: Optional[int] = None) -> Optional[float]:
        """Read one axis back from the DEVICE, in coordinator units.

        Read formats (patch): rfatt -> "... attenuation <att> dB";
        prbcap -> "DL PRB cap <n>" + per-UE rows ONLY for capped UEs
        (an absent row means uncapped); sched_prio -> "UE %04x PF weight
        <w>" rows; mcs -> "DL MCS cap [<min>..<max>] ...".
        Returns None when the device cannot be read/parsed.
        """
        if axis == "power_offset":
            ok, out = self._telnet_cmd(state.host, "ci rfatt")
            m = re.search(r"attenuation\s+([-\d.]+)", out) if ok else None
            return (BASE_ATT_DB - float(m.group(1))) if m else None
        if axis == "prb":
            ok, out = self._telnet_cmd(state.host, "ci prbcap")
            if not ok:
                return None
            if rnti is not None:
                m = re.search(rf"UE {int(rnti):04x} DL PRB cap (\d+)", out)
                # per-UE rows are printed only when capped: absent = uncapped
                return float(m.group(1)) if m else float(PRB_UNCAPPED)
            m = re.search(r"DL PRB cap (\d+)", out)
            return float(m.group(1)) if m else None
        if axis == "sched_priority":
            ok, out = self._telnet_cmd(state.host, "ci sched_prio")
            if not ok:
                return None
            if rnti is not None:
                m = re.search(rf"UE {int(rnti):04x} PF weight ([\d.]+)", out)
                return float(m.group(1)) if m else None
            rows = re.findall(r"UE [0-9a-fA-F]+ PF weight ([\d.]+)", out)
            if len(rows) == 1:
                return float(rows[0])
            # the patched gNB has NO cell-wide weight object - only per-UE
            # weights (audited individually). With 0 or >1 UEs the cell-level
            # mirror cannot be falsified from the device.
            return UNOBSERVABLE
        if axis == "mcs_offset":
            ok, out = self._telnet_cmd(state.host, "ci mcs")
            m = re.search(r"DL MCS cap \[(\d+)\.\.(\d+)\]", out) if ok else None
            return (float(m.group(2)) - BASE_MCS_CAP) if m else None
        return None

    def verify_axis(self, gnb_id: str, axis: str, expected,
                    rnti: Optional[int] = None) -> bool:
        """Read-back verification: device value == expected within _AXIS_EPS.

        This is the measured invariant behind Prop. 1's deterministic
        rollback: state equivalence is VERIFIED on the device, not assumed
        from the local mirror.
        """
        state = self.states.get(gnb_id)
        if not state:
            return False
        readback = self._read_axis(state, axis, rnti=rnti)
        if readback is UNOBSERVABLE:
            # cannot be falsified from the device in this topology; the
            # observable (per-UE) form of the axis is verified separately
            logger.debug(f"{gnb_id}: verify {axis}: unobservable, skipped")
            return True
        if readback is None:
            logger.warning(f"{gnb_id}: verify {axis}: readback failed")
            return False
        ok = abs(float(expected) - readback) <= self._AXIS_EPS[axis]
        if not ok:
            tag = "" if rnti is None else f"@{int(rnti):04x}"
            logger.error(f"{gnb_id}: verify {axis}{tag} MISMATCH: "
                         f"device={readback:g} expected={float(expected):g}")
        return ok

    def get_axis(self, gnb_id: str, axis: str, rnti: Optional[int] = None):
        """Current cached value of one axis (coordinator units); per-UE with `rnti`."""
        state = self.states.get(gnb_id)
        return state.axis_value(axis, rnti) if state else None

    def qualify_scheduler_priority(self, gnb_id: str, rnti: Optional[int] = None,
                                   test_weight: float = 2.0,
                                   kpi_probe=None) -> Dict:
        """Actuator qualification for the scheduler-priority axis (P0-18).

        Proves the scheduler-priority knob is NOT a no-op BEFORE it is admitted:
        it issues the CANONICAL 'ci sched_prio' write, READS BACK the applied
        value, confirms the readback matches the requested value and differs from
        the neutral weight (a real change), then RESTORES the original value and
        readback-verifies the restore.

        KPI-EFFECT GATING (emulated efficacy vs hardware-unverified): OVERALL
        ``qualified`` REQUIRES a measured KPI effect. When a ``kpi_probe``
        callable is supplied (the EMULATED path - it returns the addressed UE's
        throughput NOW), the qualification requires a real, correctly-signed KPI
        change (a higher PF weight => higher KPI), so a knob that reads back
        correctly but has NO scheduling effect (a silently-wrong no-op) is
        REJECTED. Without a probe (real hardware) the OTA scheduling efficacy
        cannot be observed, so ``qualified`` is False and only the separate
        ``readback_qualified`` code-path fact can be True. ``hw_efficacy_verified``
        is ALWAYS False: proving efficacy on the emulated channel is NOT proving
        it over the air.

        Fail-closed restore (ambiguous-applied): the canonical write is inside a
        try/finally, and the finally ALWAYS attempts the trusted-original restore
        after ANY write ATTEMPT (a write that returned False or raised may have
        PARTIALLY applied). Restore write/verify exceptions are caught, latch the
        executor, and leave the axis unqualified."""
        result = {"axis": "sched_priority", "gnb_id": gnb_id, "rnti": rnti,
                  "requested": None, "readback": None, "is_no_op": None,
                  "write_attempted": False, "write_ok": False,
                  "readback_matches": False, "restored": False,
                  "readback_qualified": False, "qualified": False,
                  "kpi_before": None, "kpi_after": None,
                  "kpi_effect_verified": False,
                  "hw_efficacy_verified": False,
                  "canonical_command": "ci sched_prio"}
        state = self.states.get(gnb_id)
        if state is None:
            result["error"] = f"unconfigured gNB {gnb_id!r}"
            return result
        original = self.get_sched_priority(gnb_id, rnti=rnti)
        if original is None:
            # NO trusted restore baseline: the current value is unobservable, so
            # a write we could NOT later verify-restore would be fail-open.
            # Refuse BEFORE any write - zero write, unqualified, and NO latch
            # (nothing was touched, so the device is still at its real state).
            result["error"] = (f"no readable baseline sched_priority on {gnb_id}"
                               " (unobservable) - refusing to write")
            return result
        # choose a test weight guaranteed to RAISE the PF weight above the current
        # value, so a no-op cannot masquerade as qualified AND the KPI effect has
        # a KNOWN sign (higher weight => higher KPI for a contended UE).
        req = float(test_weight)
        if original is not None and req <= float(original) + self._AXIS_EPS[
                "sched_priority"]:
            req = round(float(original) + 0.5, 3)
        result["requested"] = req

        def _probe():
            if kpi_probe is None:
                return None
            try:
                v = kpi_probe()
                return None if v is None else float(v)
            except Exception:
                return None

        # KPI baseline BEFORE the write (emulation-only; None on hardware).
        result["kpi_before"] = _probe()
        try:
            # a write ATTEMPT has UNKNOWN side-effect status the moment it is
            # issued (it may partially apply even on a False return or an
            # exception), so mark it attempted BEFORE the call - the finally then
            # ALWAYS restores.
            result["write_attempted"] = True
            write_ret = False
            try:
                write_ret = bool(self.set_sched_priority(gnb_id, req, rnti=rnti))
            except Exception as e:
                result["error"] = f"canonical sched_prio write raised: {e}"
            result["write_ok"] = write_ret
            if not write_ret:
                result.setdefault("error", "canonical sched_prio write failed")
            else:
                readback = self.get_sched_priority(gnb_id, rnti=rnti)
                result["readback"] = readback
                result["readback_matches"] = bool(
                    self.verify_axis(gnb_id, "sched_priority", req, rnti=rnti))
                # non-no-op: the applied value must actually differ from neutral.
                result["is_no_op"] = (readback is None or
                                      abs(float(readback) - PRIO_NEUTRAL) <=
                                      self._AXIS_EPS["sched_priority"])
                # KPI effect AFTER the write: a real, correctly-signed change
                # proves scheduling efficacy ON THE EMULATED CHANNEL (Gate D
                # stays open - OTA efficacy is never proven here).
                result["kpi_after"] = _probe()
                if result["kpi_before"] is not None \
                        and result["kpi_after"] is not None:
                    result["kpi_effect_verified"] = bool(
                        result["kpi_after"] - result["kpi_before"] > 1e-6)
        finally:
            # AMBIGUOUS-APPLIED (P0-18): a write ATTEMPT - even one that returned
            # False or raised - may have partially applied, so ALWAYS attempt the
            # trusted-original restore. The restore is readback-verified; a
            # restore that cannot be verified, RAISES, or has no trusted baseline
            # (original unobservable) FAILS CLOSED: the executor latches and the
            # axis is left unqualified.
            restored = False
            if result["write_attempted"]:
                try:
                    if original is not None:
                        self.set_sched_priority(gnb_id, float(original),
                                                rnti=rnti)
                        restored = bool(self.verify_axis(
                            gnb_id, "sched_priority", float(original),
                            rnti=rnti))
                except Exception as e:
                    logger.error(
                        f"sched_priority qualification restore raised: {e}")
                    restored = False
                if not restored:
                    self.latch_failsafe(
                        "sched_priority qualification restore unverified on "
                        + gnb_id + (f"/{rnti:#06x}" if rnti is not None else ""))
            else:
                restored = True   # no write attempted -> baseline intact
            result["restored"] = restored
        # code-path (readback) qualification: applied + read back + non-no-op +
        # restored. This ALONE is NOT sufficient to admit the axis.
        result["readback_qualified"] = bool(
            result["write_ok"] and result["readback_matches"]
            and not result["is_no_op"] and result["restored"])
        # OVERALL qualification REQUIRES a measured KPI effect (emulated
        # efficacy). Hardware readback-only (no probe) is NEVER qualified; OTA
        # efficacy stays unverified (hw_efficacy_verified is always False).
        result["qualified"] = bool(result["readback_qualified"]
                                   and result["kpi_effect_verified"])
        return result

    def apply_corrections(self, corrections) -> Dict[str, bool]:
        """Apply a list of LLM-proposed corrections.

        Each correction: {"gnb_id": "gnb1"|"gnb2", "delta_gain": float}
        (delta_gain is relative to the CURRENT offset, matching the ICC
        prompt schema's corrections format).
        """
        results = {}
        for corr in corrections or []:
            gnb_id = corr.get("gnb_id") or corr.get("enb_id")
            if gnb_id not in self.states:
                logger.error(f"correction for unknown gNB: {corr}")
                continue
            delta = float(corr.get("delta_gain", 0.0))
            new_offset = self.states[gnb_id].power_offset_db + delta
            results[gnb_id] = self.set_power_offset(gnb_id, new_offset)
        return results

    # ------------------------------------------------------------------ #
    # RNTI integrity guard (P8)                                            #
    # ------------------------------------------------------------------ #

    def _read_reestab_count(self, state: GNBControlState) -> Optional[int]:
        """Current RRC re-establishment counter (stock ci command);
        None when unavailable (guard disables itself gracefully)."""
        ok, out = self._telnet_cmd(state.host, "ci get_reestab_count")
        if not ok:
            return None
        m = re.search(r"reestab count\s+(\d+)", out) \
            or re.fullmatch(r"\s*(\d+)\s*", out)
        return int(m.group(1)) if m else None

    def _guard_rnti(self, gnb_id: str, rnti: int,
                    baseline: Optional[int] = None) -> Optional[int]:
        """Detect RRC re-establishment and reinterpret a stale RNTI.

        Compares the device's reestab counter against `baseline` (explicit,
        e.g. from a trial snapshot) or the last observed value. Unchanged ->
        the RNTI is trusted. Changed -> reinterpretation is attempted
        (registry resolver, then get_single_rnti, then listing diff); on
        success the mirror is migrated and the NEW RNTI returned, otherwise
        None (caller fails the axis safely). A gNB without the counter
        disables the guard (legacy behavior).
        """
        state = self.states.get(gnb_id)
        if state is None:
            return None
        current = self._read_reestab_count(state)
        if current is None:
            return rnti                    # no counter -> no guard possible
        reference = baseline if baseline is not None else state.reestab_count
        state.reestab_count = current
        if reference is None or current == reference:
            return rnti
        # re-establishment happened: the RNTI may be stale
        new_rnti = self._reinterpret_rnti(gnb_id, rnti)
        if new_rnti is None:
            reason = (f"reestab count {reference} -> {current}; could not "
                      f"reinterpret RNTI {rnti:04x} (0/multiple candidates)")
            logger.error(f"{gnb_id}: RNTI guard: {reason}")
            self.last_rnti_guard_events.append(
                {"gnb": gnb_id, "old": rnti, "new": None, "reason": reason})
            return None
        if new_rnti != rnti:
            self._migrate_rnti(gnb_id, state, rnti, new_rnti)
            self.last_rnti_guard_events.append(
                {"gnb": gnb_id, "old": rnti, "new": new_rnti,
                 "reason": f"reestab count {reference} -> {current}; "
                           f"remapped {rnti:04x} -> {new_rnti:04x}"})
        return new_rnti

    def _reinterpret_rnti(self, gnb_id: str, old_rnti: int) -> Optional[int]:
        """Map a stale RNTI to the post-re-establishment one.

        Order: (1) the injected registry resolver (coordinator's UE->RNTI
        registration map), (2) get_single_rnti (unambiguous on a single-UE
        cell), (3) listing diff - exactly one unknown RNTI present on the
        device while the old one vanished.
        """
        if self.rnti_resolver is not None:
            try:
                r = self.rnti_resolver(gnb_id, old_rnti)
                if r is not None:
                    return int(r)
            except Exception as e:
                logger.debug(f"rnti_resolver failed: {e}")
        r = self.get_connected_rnti(gnb_id)
        if r is not None:
            return r
        state = self.states[gnb_id]
        ok, out = self._telnet_cmd(state.host, "ci sched_prio")
        if not ok:
            return None
        device = {int(x, 16)
                  for x in re.findall(r"UE ([0-9a-fA-F]+) PF weight", out)}
        known = set(state.ue_sched_priority) | set(state.ue_prb_cap)
        unknown = device - known
        if old_rnti not in device and len(unknown) == 1:
            return next(iter(unknown))
        return None

    def _migrate_rnti(self, gnb_id: str, state: GNBControlState,
                      old: int, new: int):
        """Move the mirror's per-UE entries to the new RNTI and notify."""
        if old in state.ue_sched_priority:
            state.ue_sched_priority[new] = state.ue_sched_priority.pop(old)
        if old in state.ue_prb_cap:
            state.ue_prb_cap[new] = state.ue_prb_cap.pop(old)
        logger.info(f"{gnb_id}: RNTI remapped {old:04x} -> {new:04x} "
                    f"(RRC re-establishment)")
        if self.on_rnti_remap is not None:
            try:
                self.on_rnti_remap(gnb_id, old, new)
            except Exception as e:
                logger.debug(f"on_rnti_remap callback failed: {e}")

    # snapshot value tolerance for the diff-based restore (floats)
    _AXIS_EPS = {"power_offset": 1e-6, "prb": 0.5,
                 "sched_priority": 1e-4, "mcs_offset": 1e-6}

    def _read_per_ue_prb(self, state: GNBControlState):
        """Full per-UE DL PRB cap map from the device (mirror convention:
        absent RNTI = uncapped; the patch prints rows only for capped UEs).
        None when the read fails or the command does not exist (stock gNB) -
        the argless form always prints at least the cell line, so a missing
        "DL PRB cap" marker means the command is not the patched one.
        """
        ok, out = self._telnet_cmd(state.host, "ci prbcap")
        if not ok or "DL PRB cap" not in out:
            return None
        return {int(r, 16): int(n) for r, n in
                re.findall(r"UE ([0-9a-fA-F]+) DL PRB cap (\d+)", out)}

    def _read_per_ue_sched(self, state: GNBControlState):
        """Full per-UE PF weight override map from the device (mirror
        convention: a neutral weight is not an override). None when the read
        fails or the command is not the patched one. The patched argless
        form prints ONE ROW PER UE and nothing else - with zero UEs the
        output is legitimately empty (the simulator prints "no UEs
        connected"), so only unrecognized non-empty text is a failure.
        """
        ok, out = self._telnet_cmd(state.host, "ci sched_prio")
        if not ok:
            return None
        stripped = out.strip()
        if stripped and "PF weight" not in out and "no UEs" not in out:
            return None   # unknown-command / error text
        return {int(r, 16): float(w) for r, w in
                re.findall(r"UE ([0-9a-fA-F]+) PF weight ([\d.]+)", out)
                if abs(float(w) - PRIO_NEUTRAL)
                > self._AXIS_EPS["sched_priority"]}

    def _sync_mirror_from_device(self, gnb_id: str, state: GNBControlState
                                 ) -> Dict[str, str]:
        """Synchronize the local mirror to the DEVICE state, axis by axis.

        Returns per-axis provenance {"axis": "device" | "mirror"} ("mirror"
        = the read failed or the axis is unobservable, so the mirror value
        was kept). Any drift between mirror and device is logged - it means
        something else wrote to the gNB (manual telnet, restart, ...).
        """
        source: Dict[str, str] = {}
        for axis in ("power_offset", "prb", "sched_priority", "mcs_offset"):
            rb = self._read_axis(state, axis)
            if rb is None or rb is UNOBSERVABLE:
                source[axis] = "mirror"
                continue
            cur = state.axis_value(axis)
            if abs(float(rb) - float(cur)) > self._AXIS_EPS[axis]:
                logger.warning(
                    f"{gnb_id}: pre-trial mirror drift on {axis}: "
                    f"mirror={float(cur):g} device={float(rb):g} - "
                    f"snapshotting the DEVICE state")
            if axis == "power_offset":
                state.tx_att_db = BASE_ATT_DB - float(rb)
            elif axis == "prb":
                state.prb_cap = int(rb)
            elif axis == "sched_priority":
                state.sched_priority = float(rb)
            elif axis == "mcs_offset":
                state.mcs_cap = int(round(BASE_MCS_CAP + float(rb)))
            source[axis] = "device"
        # per-UE maps: each axis independently (a sched_prio read failure
        # must not discard a valid device PRB reading, and vice versa)
        prb_map = self._read_per_ue_prb(state)
        if prb_map is None:
            source["ue_prb"] = "mirror"
        else:
            if prb_map != state.ue_prb_cap:
                logger.warning(f"{gnb_id}: pre-trial per-UE PRB mirror drift "
                               f"- snapshotting the DEVICE state")
            state.ue_prb_cap = prb_map
            source["ue_prb"] = "device"
        sched_map = self._read_per_ue_sched(state)
        if sched_map is None:
            source["ue_sched"] = "mirror"
        else:
            if sched_map != state.ue_sched_priority:
                logger.warning(f"{gnb_id}: pre-trial per-UE PF-weight mirror "
                               f"drift - snapshotting the DEVICE state")
            state.ue_sched_priority = sched_map
            source["ue_sched"] = "device"
        return source

    def snapshot(self, from_device: bool = True) -> Dict[str, Dict[str, float]]:
        """Full multi-axis snapshot of every gNB, for deterministic rollback.

        With from_device=True (default) every readable axis - per-UE
        overrides included - is READ BACK FROM THE DEVICE first and the
        mirror is synchronized to it, so the snapshot is the true pre-trial
        DEVICE state, not the mirror's belief (C6: a stale mirror used to
        poison the snapshot - device at +5 dB / mirror at 0 dB snapshotted
        0 dB, and the "verified" restore then faithfully restored the wrong
        state). Unreadable axes (stock gNB without the scheduler patches;
        cell PF weight with 0/>1 UEs) keep the mirror value; the per-axis
        provenance is recorded under "snapshot_source".

        Returns a nested dict {gnb_id: {power_offset_db, prb_cap, sched_priority,
        mcs_offset, ue_sched_priority, ue_prb_cap, snapshot_source,
        reestab_count}} (the ue_* entries are RNTI->value dicts of per-UE
        overrides; reestab_count is the P8 RNTI-integrity baseline, None when
        the gNB has no counter). `restore()` accepts this form and also the
        legacy flat {gnb_id: offset_db} form (power-only), so old callers
        keep working.
        """
        out = {}
        for gid, s in self.states.items():
            if from_device:
                source = self._sync_mirror_from_device(gid, s)
            else:
                source = {a: "mirror" for a in
                          ("power_offset", "prb", "sched_priority",
                           "mcs_offset", "ue_prb", "ue_sched")}
            snap = s.as_snapshot()
            snap["snapshot_source"] = source
            count = self._read_reestab_count(s)
            snap["reestab_count"] = count
            if count is not None:
                s.reestab_count = count
            out[gid] = snap
        return out

    def restore(self, snapshot: Dict) -> bool:
        """Deterministic rollback to a previously saved snapshot (all axes),
        followed by a FULL device read-back audit (P7 verified actuation).

        Diff-based re-apply: an axis is re-sent only when its snapshot value
        differs from the current cached value. This makes rollback
        idempotent, keeps the power-only path working on a gNB that has NOT
        been rebuilt with the scheduler patches (the untouched PRB/priority/
        MCS axes stay neutral and are never re-sent), and still forces every
        changed axis back to the snapshot value.

        After re-application, every axis the rollback actually re-applied
        (per-RNTI included) is read back from the device and compared
        against the snapshot; the audit is stored in
        self.last_restore_report as {gnb: {axis | "axis@rnti":
        (snapshot, readback, ok)}} and restore() returns True only when the
        whole report matches ("state equivalence verified on every observed
        rollback"). Auditing only the TOUCHED axes keeps the power-only
        path working on a stock gNB, where the prbcap/sched_prio/mcs read
        commands do not exist.
        """
        # Batch B (P0-1/item 6): restore IS the recovery path, so its own
        # writes must be permitted even while latched - but ONLY on the calling
        # thread. The authorization is thread-local, so a concurrent thread
        # still sees the latch and is refused.
        local = getattr(self, "_restore_local", None)
        if local is None:
            local = self._restore_local = threading.local()
        prev = getattr(local, "active", False)
        local.active = True
        try:
            return self._restore_locked(snapshot)
        finally:
            local.active = prev

    def _restore_locked(self, snapshot: Dict) -> bool:
        ok = True
        touched: Dict[str, set] = {}
        remaps: Dict[str, Dict[int, int]] = {}
        for gnb_id, snap in (snapshot or {}).items():
            state = self.states.get(gnb_id)
            if not state:
                logger.error(f"restore: unknown gNB {gnb_id}")
                ok = False
                continue

            if not isinstance(snap, dict):
                # Legacy flat form {gnb_id: offset_db} (power only)
                snap = {"power_offset_db": float(snap)}

            targets = self._snapshot_targets(snap)
            for axis, target in targets.items():
                if target is None:
                    continue
                current = state.axis_value(axis)
                if abs(float(target) - float(current)) <= self._AXIS_EPS[axis]:
                    continue  # unchanged -> nothing to roll back on this axis
                touched.setdefault(gnb_id, set()).add(axis)
                # per-axis verify skipped: the audit below reads back every
                # touched axis once anyway
                ok = self.apply_axis(gnb_id, axis, target, verify=False) and ok

            # Per-UE overrides: roll back every RNTI touched since the snapshot.
            # A UE present now but absent in the snapshot was overridden during
            # the trial, so its target is the axis neutral (reset it); a UE in the
            # snapshot is restored to its snapshotted value. Diff-based, so an
            # un-patched gNB (no per-UE state ever set) is never touched.
            for axis, snap_key, neutral in (
                ("sched_priority", "ue_sched_priority", PRIO_NEUTRAL),
                ("prb", "ue_prb_cap", PRB_UNCAPPED),
            ):
                snap_ue = snap.get(snap_key) or {}
                # P8: remaps recorded while restoring an EARLIER axis rekey
                # this axis's snapshot entries too - otherwise the union
                # visits a logical UE twice (old + new RNTI) and the second
                # pass resets the just-restored value to neutral
                rmap = remaps.get(gnb_id, {})
                snap_ue = {rmap.get(int(r), int(r)): v
                           for r, v in snap_ue.items()}
                cur_ue = (state.ue_sched_priority if axis == "sched_priority"
                          else state.ue_prb_cap)
                for rnti in set(snap_ue) | set(cur_ue):
                    target = snap_ue.get(rnti, neutral)
                    current = state.axis_value(axis, rnti)
                    if abs(float(target) - float(current)) <= self._AXIS_EPS[axis]:
                        continue
                    # P8: re-establishment during the trial changes the RNTI;
                    # reinterpret against the snapshot's counter baseline or
                    # fail this axis safely
                    guarded = self._guard_rnti(gnb_id, int(rnti),
                                               baseline=snap.get("reestab_count"))
                    if guarded is None:
                        ok = False
                        continue
                    if guarded != int(rnti):
                        remaps.setdefault(gnb_id, {})[int(rnti)] = guarded
                    # audit under the CURRENT (possibly remapped) RNTI - the
                    # mirror has been migrated, so the union below sees it
                    touched.setdefault(gnb_id, set()).add(
                        f"{axis}@{int(guarded):04x}")
                    ok = self.apply_axis(gnb_id, axis, target, rnti=guarded,
                                         verify=False) and ok

        return self._audit_restore(snapshot, touched, remaps) and ok

    @staticmethod
    def _snapshot_targets(snap: Dict) -> Dict[str, Optional[float]]:
        return {
            "power_offset": snap.get("power_offset_db"),
            "prb": snap.get("prb_cap"),
            "sched_priority": snap.get("sched_priority"),
            "mcs_offset": snap.get("mcs_offset"),
        }

    def _audit_restore(self, snapshot: Dict,
                       touched: Optional[Dict[str, set]] = None,
                       remaps: Optional[Dict[str, Dict[int, int]]] = None
                       ) -> bool:
        """Read back snapshot axes from the device and diff against the
        snapshot. With `touched` given, only the axes the rollback actually
        re-applied are audited (the state the trial moved - and the only
        commands guaranteed to exist on a stock gNB); without it, every
        snapshot axis is audited. Stores self.last_restore_report;
        True iff all audited entries match."""
        report: Dict[str, Dict] = {}
        verified = True

        def _wanted(gnb_id: str, key: str) -> bool:
            return touched is None or key in (touched.get(gnb_id) or ())

        for gnb_id, snap in (snapshot or {}).items():
            state = self.states.get(gnb_id)
            if not state:
                verified = False   # cannot audit what we cannot address
                continue
            if not isinstance(snap, dict):
                snap = {"power_offset_db": float(snap)}
            entries: Dict[str, Tuple] = {}
            for axis, target in self._snapshot_targets(snap).items():
                if target is None or not _wanted(gnb_id, axis):
                    continue
                if axis == "sched_priority" and (
                        (snap.get("ue_sched_priority") or {})
                        or state.ue_sched_priority):
                    # the device has ONLY per-UE weights; once per-UE
                    # overrides exist the cell-level mirror is fictional -
                    # the per-UE entries below audit the observable state
                    entries[axis] = (float(target), None, True)
                    continue
                rb = self._read_axis(state, axis)
                if rb is UNOBSERVABLE:
                    # no device object to diff (e.g. cell PF weight on a
                    # multi-UE cell); the per-UE entries below audit the
                    # observable state
                    entries[axis] = (float(target), None, True)
                    continue
                ok_axis = (rb is not None
                           and abs(float(target) - rb) <= self._AXIS_EPS[axis])
                entries[axis] = (float(target), rb, ok_axis)
                verified = verified and ok_axis
            for axis, snap_key, neutral in (
                ("sched_priority", "ue_sched_priority", PRIO_NEUTRAL),
                ("prb", "ue_prb_cap", PRB_UNCAPPED),
            ):
                snap_ue = snap.get(snap_key) or {}
                # P8: snapshot entries keyed by a pre-re-establishment RNTI
                # are audited under the remapped (current) RNTI
                rmap = (remaps or {}).get(gnb_id, {})
                snap_ue = {rmap.get(int(r), int(r)): v
                           for r, v in snap_ue.items()}
                cur_ue = (state.ue_sched_priority if axis == "sched_priority"
                          else state.ue_prb_cap)
                for rnti in set(snap_ue) | set(cur_ue):
                    key = f"{axis}@{int(rnti):04x}"
                    if not _wanted(gnb_id, key):
                        continue
                    target = snap_ue.get(rnti, neutral)
                    rb = self._read_axis(state, axis, rnti=rnti)
                    ok_axis = (rb is not None and rb is not UNOBSERVABLE and
                               abs(float(target) - rb) <= self._AXIS_EPS[axis])
                    entries[key] = (float(target), rb, ok_axis)
                    verified = verified and ok_axis
            report[gnb_id] = entries
            if not all(e[2] for e in entries.values()):
                logger.error(f"restore audit MISMATCH on {gnb_id}: "
                             f"{ {k: v for k, v in entries.items() if not v[2]} }")
        self.last_restore_report = report
        return verified

    def reset_gains(self) -> bool:
        """Reset all gNBs to the 0 dB power offset baseline (power axis only)."""
        ok = True
        for gnb_id in self.states.keys():
            ok = self.set_power_offset(gnb_id, 0.0) and ok
        return ok

    def reset_all_axes(self) -> bool:
        """Reset every gNB to the neutral action vector on all d>=2 axes,
        including any per-UE (per-RNTI) overrides."""
        ok = True
        for gnb_id, state in self.states.items():
            ok = self.set_power_offset(gnb_id, 0.0) and ok
            ok = self.set_prb_allocation(gnb_id, PRB_UNCAPPED) and ok
            ok = self.set_sched_priority(gnb_id, PRIO_NEUTRAL) and ok
            ok = self.set_mcs_offset(gnb_id, 0.0) and ok
            # Reset each per-UE override that is not already neutral.
            for rnti, w in list(state.ue_sched_priority.items()):
                if abs(w - PRIO_NEUTRAL) > self._AXIS_EPS["sched_priority"]:
                    ok = self.set_sched_priority(gnb_id, PRIO_NEUTRAL, rnti=rnti) and ok
            for rnti, p in list(state.ue_prb_cap.items()):
                if p != PRB_UNCAPPED:
                    ok = self.set_prb_allocation(gnb_id, PRB_UNCAPPED, rnti=rnti) and ok
        return ok

    # ------------------------------------------------------------------ #
    # handover / diagnostics                                              #
    # ------------------------------------------------------------------ #

    def trigger_handover(self, source_gnb: str, target_gnb: str,
                         ue_id: int = 1) -> bool:
        """Trigger an N2 handover of a UE from source to target gNB."""
        # Batch B (item 7): a handover is an ORDINARY write to the RAN and must
        # obey the latch - a latched executor refuses it (read-only diagnostics
        # stay allowed).
        if self._write_blocked():
            logger.error("trigger_handover refused - executor latched")
            return False
        src = self.states.get(source_gnb)
        tgt = self.states.get(target_gnb)
        if not src or not tgt:
            logger.error(f"Unknown gNB: {source_gnb} or {target_gnb}")
            return False
        success, output = self._telnet_cmd(
            src.host, f"ci trigger_n2_ho {tgt.pci} {ue_id}")
        logger.info(f"N2 HO {source_gnb}->{target_gnb} (pci {tgt.pci}): {output}")
        return success

    def get_connected_rnti(self, gnb_id: str) -> Optional[int]:
        """RNTI of the single connected UE on a gNB (None if 0 or >1 UEs)."""
        state = self.states.get(gnb_id)
        if not state:
            return None
        success, output = self._telnet_cmd(state.host, "ci get_single_rnti")
        if success:
            m = re.search(r"RNTI\s+([0-9a-fA-F]+)", output)
            if m:
                return int(m.group(1), 16)
        return None

    def is_reachable(self, gnb_id: str) -> bool:
        """True if the gNB telnet server answers."""
        state = self.states.get(gnb_id)
        if not state:
            return False
        success, _ = self._telnet_cmd(state.host, "ci rfatt", timeout=2.0)
        return success

    def close(self):
        """Kept for interface compatibility (connections are per-command)."""


def create_executor_from_config(config=None) -> OAIExecutor:
    """Create an OAIExecutor from a Config object (or defaults)."""
    if config is None:
        return OAIExecutor()
    gnb_configs = {}
    for gnb_id, gnb_config in config.network.gnbs.items():
        host = "127.0.0.1" if getattr(gnb_config, "is_local", False) \
            else getattr(gnb_config, "ip", None) or gnb_config.hostname
        gnb_configs[gnb_id] = {"host": host, "pci": gnb_config.pci}
    return OAIExecutor(gnb_configs)
