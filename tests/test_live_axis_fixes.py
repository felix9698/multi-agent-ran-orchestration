#!/usr/bin/env python3
"""
Offline unit tests for the Phase-B live-axis fixes (2026-07-27).

NO live RAN / NO hardware. Everything runs against a mocked OAI telnet
transport or a mocked socket layer, per CLAUDE.md ("모든 검증은 mock/emulation").

Covers:
  * Task 1 - per-gNB `sched_priority` fan-out on a shared cell (UE1+UE2 -> gNB1):
    single-UE unchanged, multi-UE fan-out, 0-UE fail, atomic partial-failure
    rollback, transport-failure guard, and snapshot/restore of fanned-out
    per-UE weights.
  * Task 2 - per-host telnet backoff: connect-failure suppression, window
    expiry retry, auto-recovery, and "read error is not a connect failure".
  * Task 3 (R4) - LIVE_UE_SET operator-declared UE subset opens the live-start
    topology gate without touching the tracked default topology.

Run:  python3 -m unittest tests.test_live_axis_fixes
"""

import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import executor.oai_executor as oe
from executor.oai_executor import OAIExecutor, PRIO_NEUTRAL


# --------------------------------------------------------------------------- #
# Fake telnet modelling the PATCHED gNB's per-UE PF-weight scheduler.          #
#                                                                              #
# The real patched telnet keeps ONE PF weight per UE (no cell-wide object), so #
# the argless cell write `ci sched_prio <w>` applies only on a single-UE cell  #
# and FAILS CLOSED ("could not identify UE ... multiple UEs") otherwise. That  #
# is exactly the behavior that makes a cell-level LLM proposal need fan-out.    #
# --------------------------------------------------------------------------- #
def install_patched_sched_telnet(ex, connected, fail_rnti=None,
                                 cell_write_transport_fail=False):
    """Monkeypatch ex._telnet_cmd; returns the (host, cmd) record list.

    `connected` is a MUTABLE {rnti_int: weight_float} of attached UEs, updated
    in place by writes so read-backs stay consistent (mirrors the device).
    """
    sent = []

    def fake(self, host, cmd, timeout=3.0):
        sent.append((host, cmd))
        parts = cmd.split()
        # ---- sched_prio -------------------------------------------------- #
        if cmd == "ci sched_prio":                       # argless read (list)
            if not connected:
                return True, ""                          # patched gNB, 0 UEs
            return True, "\n".join(f"UE {r:04x} PF weight {w:.3f}"
                                   for r, w in sorted(connected.items()))
        if parts[:2] == ["ci", "sched_prio"] and len(parts) == 3:   # cell write
            if cell_write_transport_fail:
                return False, "[Errno 111] Connection refused"
            w = float(parts[2])
            if len(connected) == 1:                      # single UE -> applies
                r = next(iter(connected))
                connected[r] = w
                return True, f"UE {r:04x} PF weight set to {w:.3f}"
            # 0 or >1 UEs: the patched gNB fails closed (no "set to")
            return True, ("could not identify UE (no UE, no such RNTI, or "
                          "multiple UEs)")
        if parts[:2] == ["ci", "sched_prio"] and len(parts) == 4:   # per-UE
            w = float(parts[2])
            r = int(parts[3], 16)
            if fail_rnti is not None and r == fail_rnti:
                return True, "could not identify UE (no such RNTI)"
            connected[r] = w
            return True, f"UE {r:04x} PF weight set to {w:.3f}"
        # ---- other axes / diagnostics needed by snapshot/restore --------- #
        if cmd == "ci rfatt":
            return True, "current TX attenuation 12.0 dB"
        if cmd == "ci prbcap":
            return True, "DL PRB cap 0 (uncapped)"
        if cmd == "ci mcs":
            return True, "DL MCS cap [0..28] UL MCS cap [0..28]"
        if cmd == "ci get_reestab_count":
            return True, "reestab count 0"
        return True, "ok"

    ex._telnet_cmd = types.MethodType(fake, ex)
    return sent


class TestSchedPriorityFanout(unittest.TestCase):
    UE1 = 0x2c0a
    UE2 = 0xac3b

    def _executor(self, connected, **kw):
        # single-gNB, matching the current live bring-up (UE1+UE2 -> gNB1 only;
        # gNB2/PC2 absent), so snapshot/restore touch exactly the cell under test
        ex = OAIExecutor(gnb_configs={"gnb1": {"host": "127.0.0.1", "pci": 0}})
        sent = install_patched_sched_telnet(ex, connected, **kw)
        return ex, sent

    def test_single_ue_cell_write_unchanged(self):
        """1 UE on the cell: the canonical argless write applies directly and
        NO per-UE fan-out happens (wire behavior identical to before)."""
        connected = {0x4601: 1.0}
        ex, sent = self._executor(connected)
        self.assertTrue(ex.set_sched_priority("gnb1", 2.0))
        # exactly one command, the argless cell write - no enumeration/fan-out
        self.assertEqual(sent, [("127.0.0.1", "ci sched_prio 2.000")])
        self.assertAlmostEqual(ex.states["gnb1"].sched_priority, 2.0)
        self.assertEqual(connected[0x4601], 2.0)
        self.assertEqual(ex.states["gnb1"].ue_sched_priority, {})

    def test_two_ue_cell_write_fans_out(self):
        """2 UEs share the cell: the argless write is refused, so the weight is
        fanned out per-UE to BOTH connected UEs."""
        connected = {self.UE1: 1.0, self.UE2: 1.0}
        ex, sent = self._executor(connected)
        self.assertTrue(ex.set_sched_priority("gnb1", 2.0))
        cmds = [c for _, c in sent]
        self.assertIn("ci sched_prio 2.000", cmds)              # argless attempt
        self.assertIn("ci sched_prio 2.000 2c0a", cmds)         # fan-out UE1
        self.assertIn("ci sched_prio 2.000 ac3b", cmds)         # fan-out UE2
        self.assertEqual(connected, {self.UE1: 2.0, self.UE2: 2.0})
        self.assertEqual(ex.states["gnb1"].ue_sched_priority,
                         {self.UE1: 2.0, self.UE2: 2.0})
        # the cell-level mirror stays neutral: per-UE map is the source of truth
        self.assertAlmostEqual(ex.states["gnb1"].sched_priority, PRIO_NEUTRAL)

    def test_zero_ue_returns_false(self):
        """0 UEs on the cell: no UE to actuate -> fail closed (no fan-out)."""
        connected = {}
        ex, sent = self._executor(connected)
        self.assertFalse(ex.set_sched_priority("gnb1", 2.0))
        # no per-UE (4-token) command was ever issued
        self.assertFalse(any(len(c.split()) == 4 for _, c in sent))

    def test_fanout_partial_failure_rolls_back_atomically(self):
        """If a per-UE write fails mid-fan-out, the UEs already changed in this
        call are restored to their pre-call weights and False is returned."""
        connected = {self.UE1: 1.0, self.UE2: 1.0}
        ex, sent = self._executor(connected, fail_rnti=self.UE2)
        self.assertFalse(ex.set_sched_priority("gnb1", 3.0))
        # UE1 was applied then rolled back to 1.0; UE2 never changed
        self.assertEqual(connected, {self.UE1: 1.0, self.UE2: 1.0})
        self.assertAlmostEqual(ex.states["gnb1"].ue_sched_priority[self.UE1], 1.0)
        # the rollback wrote UE1 back to its previous weight
        cmds = [c for _, c in sent]
        self.assertIn("ci sched_prio 3.000 2c0a", cmds)   # applied
        self.assertIn("ci sched_prio 1.000 2c0a", cmds)   # rolled back

    def test_transport_failure_does_not_fan_out(self):
        """A telnet transport failure on the cell write must NOT trigger fan-out
        (fanning out would just fail too and hide the transport problem)."""
        connected = {self.UE1: 1.0, self.UE2: 1.0}
        ex, sent = self._executor(connected, cell_write_transport_fail=True)
        self.assertFalse(ex.set_sched_priority("gnb1", 2.0))
        # only the failed cell write; no enumeration read, no per-UE writes
        self.assertEqual(sent, [("127.0.0.1", "ci sched_prio 2.000")])

    def test_apply_axis_cell_level_fanout(self):
        """apply_axis(rnti=None) on a shared cell fans out and returns True
        (the cell-level read-back is unobservable with >1 UE, so verify is
        correctly skipped, while the per-UE writes did take effect)."""
        connected = {self.UE1: 1.0, self.UE2: 1.0}
        ex, _ = self._executor(connected)
        self.assertTrue(ex.apply_axis("gnb1", "sched_priority", 2.0))
        self.assertEqual(connected, {self.UE1: 2.0, self.UE2: 2.0})

    def test_snapshot_restore_rolls_back_fanned_out_weights(self):
        """A trial that fanned a cell weight out to 2 UEs must be rolled back
        per-RNTI by snapshot/restore, back to the pre-trial neutral state."""
        connected = {self.UE1: 1.0, self.UE2: 1.0}
        ex, _ = self._executor(connected)
        snap = ex.snapshot(from_device=True)
        # trial: cell-level proposal fans out to both UEs
        self.assertTrue(ex.apply_axis("gnb1", "sched_priority", 2.5))
        self.assertEqual(connected, {self.UE1: 2.5, self.UE2: 2.5})
        # deterministic rollback restores every UE and verifies on the device
        self.assertTrue(ex.restore(snap))
        self.assertEqual(connected, {self.UE1: 1.0, self.UE2: 1.0})

    def test_snapshot_restore_preserves_heterogeneous_baseline(self):
        """If a UE already had a non-neutral per-UE weight before the trial,
        restore returns THAT UE to its own baseline, not to neutral."""
        connected = {self.UE1: 1.0, self.UE2: 1.0}
        ex, _ = self._executor(connected)
        # pre-trial baseline: UE1 individually biased to 1.5
        self.assertTrue(ex.set_sched_priority("gnb1", 1.5, rnti=self.UE1))
        snap = ex.snapshot(from_device=True)
        # trial: cell-level fan-out overrides both to 3.0
        self.assertTrue(ex.apply_axis("gnb1", "sched_priority", 3.0))
        self.assertEqual(connected, {self.UE1: 3.0, self.UE2: 3.0})
        self.assertTrue(ex.restore(snap))
        # UE1 back to its 1.5 baseline; UE2 back to neutral
        self.assertEqual(connected, {self.UE1: 1.5, self.UE2: 1.0})


# --------------------------------------------------------------------------- #
class _FakeSock:
    """Minimal socket stand-in: always answers with an OAI prompt so
    _read_until_prompt terminates on the first recv."""
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def settimeout(self, t):
        pass

    def sendall(self, b):
        pass

    def recv(self, n):
        return b"foo_gnb> "

    def close(self):
        pass


class _ReadRaisingSock(_FakeSock):
    def recv(self, n):
        raise OSError("read boom")


class TestTelnetBackoff(unittest.TestCase):
    HOST = "192.168.0.51"

    def setUp(self):
        self.ex = OAIExecutor()
        self.ex.telnet_backoff_s = 5.0
        self.clock = [0.0]

    def _mono(self):
        return self.clock[0]

    def test_connect_failure_backs_off_and_suppresses(self):
        with mock.patch.object(oe.time, "monotonic", self._mono), \
                mock.patch.object(oe.socket, "create_connection",
                                  side_effect=OSError(111, "Connection refused")
                                  ) as conn, \
                self.assertLogs("OAIExecutor", level="WARNING") as log:
            self.clock[0] = 0.0
            ok, _ = self.ex._telnet_cmd(self.HOST, "ci rfatt")
            self.assertFalse(ok)
            self.assertEqual(conn.call_count, 1)          # first real attempt
            # within the 5 s window: subsequent calls short-circuit
            self.clock[0] = 1.0
            self.assertFalse(self.ex._telnet_cmd(self.HOST, "ci rfatt")[0])
            self.clock[0] = 4.9
            self.assertFalse(self.ex._telnet_cmd(self.HOST, "ci rfatt")[0])
            self.assertEqual(conn.call_count, 1)          # NOT retried
        warnings = [r for r in log.records if r.levelname == "WARNING"]
        self.assertEqual(len(warnings), 1)                # logged exactly once
        self.assertIn(self.HOST, self.ex._unreachable)

    def test_backoff_window_expiry_retries(self):
        with mock.patch.object(oe.time, "monotonic", self._mono), \
                mock.patch.object(oe.socket, "create_connection",
                                  side_effect=OSError(111, "refused")) as conn:
            self.clock[0] = 0.0
            self.ex._telnet_cmd(self.HOST, "ci rfatt")     # attempt 1, backoff
            self.assertEqual(conn.call_count, 1)
            self.clock[0] = 6.0                            # past the 5 s window
            self.ex._telnet_cmd(self.HOST, "ci rfatt")     # attempt 2
            self.assertEqual(conn.call_count, 2)

    def test_successful_connect_clears_backoff(self):
        # first, drive it into backoff
        with mock.patch.object(oe.time, "monotonic", self._mono), \
                mock.patch.object(oe.socket, "create_connection",
                                  side_effect=OSError(111, "refused")):
            self.clock[0] = 0.0
            self.ex._telnet_cmd(self.HOST, "ci rfatt")
        self.assertIn(self.HOST, self.ex._unreachable)
        # window expires and the host answers -> auto-recovery
        self.clock[0] = 10.0
        with mock.patch.object(oe.time, "monotonic", self._mono), \
                mock.patch.object(oe.socket, "create_connection",
                                  return_value=_FakeSock()), \
                self.assertLogs("OAIExecutor", level="INFO") as log:
            ok, _ = self.ex._telnet_cmd(self.HOST, "ci rfatt")
        self.assertTrue(ok)
        self.assertNotIn(self.HOST, self.ex._unreachable)     # cleared
        self.assertTrue(any("reachable again" in r.getMessage()
                            for r in log.records))

    def test_post_connect_read_error_is_not_backed_off(self):
        """A read error after a successful CONNECT means the host is present;
        it must not enter the connect backoff (only connect failures do)."""
        with mock.patch.object(oe.time, "monotonic", self._mono), \
                mock.patch.object(oe.socket, "create_connection",
                                  return_value=_ReadRaisingSock()):
            self.clock[0] = 0.0
            ok, _ = self.ex._telnet_cmd(self.HOST, "ci rfatt")
        self.assertFalse(ok)
        self.assertNotIn(self.HOST, self.ex._unreachable)

    def test_reachable_host_never_enters_backoff(self):
        with mock.patch.object(oe.time, "monotonic", self._mono), \
                mock.patch.object(oe.socket, "create_connection",
                                  return_value=_FakeSock()):
            self.clock[0] = 0.0
            ok, _ = self.ex._telnet_cmd(self.HOST, "ci rfatt")
        self.assertTrue(ok)
        self.assertEqual(self.ex._unreachable, {})


# --------------------------------------------------------------------------- #
class TestLiveUeSetGate(unittest.TestCase):
    """R4: LIVE_UE_SET lets the operator declare which physical UEs are present,
    opening validate_live_topology() for a provisioned subset without editing
    the tracked default 3-UE topology."""

    _SAVED = {}
    _ENV_KEYS = ("LIVE_UE_SET", "UE3_HOST", "UE3_IP", "UE3_SSH_USER", "UE3_IMSI")

    def setUp(self):
        import config
        self.config = config
        for k in self._ENV_KEYS:
            self._SAVED[k] = os.environ.pop(k, None)
        config._config = None

    def tearDown(self):
        for k in self._ENV_KEYS:
            if self._SAVED.get(k) is not None:
                os.environ[k] = self._SAVED[k]
            else:
                os.environ.pop(k, None)
        self.config._config = None

    def test_default_topology_keeps_ue3_slot_but_gate_excludes_it(self):
        # The logical topology KEEPS ue3 (an expansion slot); the live gate does
        # NOT fail closed for it - it EXCLUDES the unprovisioned slot and returns
        # the active provisioned set (ue1, ue2). Fail-closed only on 0 provisioned.
        cfg = self.config.get_default_config()
        self.assertEqual(list(cfg.network.ues), ["ue1", "ue2", "ue3"])
        active = cfg.validate_live_topology()       # must NOT raise
        self.assertEqual(active, ["ue1", "ue2"])    # ue3 excluded, not fatal

    def test_subset_opens_gate(self):
        os.environ["LIVE_UE_SET"] = "ue1,ue2"
        cfg = self.config.get_default_config()
        self.assertEqual(list(cfg.network.ues), ["ue1", "ue2"])
        self.assertEqual(cfg.validate_live_topology(), ["ue1", "ue2"])  # no raise

    def test_subset_trims_derived_topology(self):
        os.environ["LIVE_UE_SET"] = "ue1,ue2"
        cfg = self.config.get_default_config()
        from experiments.topology import from_network_config
        topo = from_network_config(cfg.network)
        self.assertEqual(topo.ue_serving, {"ue1": "gnb1", "ue2": "gnb1"})

    def test_unknown_ue_name_raises(self):
        os.environ["LIVE_UE_SET"] = "ue1,ueX"
        with self.assertRaises(ValueError):
            self.config.get_default_config()

    def test_whitespace_and_order_preserved(self):
        os.environ["LIVE_UE_SET"] = " ue2 , ue1 "
        cfg = self.config.get_default_config()
        self.assertEqual(list(cfg.network.ues), ["ue2", "ue1"])


if __name__ == "__main__":
    unittest.main()
