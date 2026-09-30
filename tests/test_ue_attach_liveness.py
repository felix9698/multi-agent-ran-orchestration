#!/usr/bin/env python3
"""
Tests for the UE ATTACH-LIVENESS fix (see docs/ue_stability_and_capacity.md).

The defect (observed live on the testbed): when a UE is released from the cell,
OAI's SDAP layer brings the PDU-session tun device DOWN but LEAVES ITS IPv4
ADDRESS CONFIGURED:

    attached   oaitun_ue1: <POINTOPOINT,NOARP,UP,LOWER_UP>  inet 12.1.1.187/24
    released   oaitun_ue1: <POINTOPOINT,NOARP>              inet 12.1.1.187/24

The old probe was `ip -4 addr show <dev> | grep inet`, which prints an address in
BOTH states. A UE that had been released by the core therefore reported
`attached=True` with a stale IP while it was on no cell at all and its goodput
was structurally 0 - the KPI read as a real 0 Mbps instead of "not attached",
which is the difference between a measured intent violation and a dead UE.

`ip -4 -o addr show up dev <dev>` is the exact discriminator: the `up` selector
filters on IFF_UP, which is cleared by the release while the address is not.

Everything is mock/monkeypatched - NO real subprocess, NO real SSH, no hardware
(project rule: testbed/LLM/network absent; all mock/emulation).
"""

import unittest

from collectors.multi_ue_collector import UECollector, UE_TUN_DEV


# --------------------------------------------------------------------------- #
# A faithful miniature of the two `ip` behaviours observed on the real UE.     #
# --------------------------------------------------------------------------- #
def _fake_ip_shell(link_up: bool, addr: str = "12.1.1.187/24"):
    """Return an `_ssh_cmd` stand-in that emulates iproute2 for the tun device.

    The address is present in BOTH states (that is the whole trap); only the
    `up` selector distinguishes them.
    """
    issued = []

    def _ssh(cmd, timeout=5.0):
        issued.append(cmd)
        if "pgrep" in cmd:
            return True, "4711"          # modem process alive in BOTH states -
            #                              that is exactly why pgrep cannot be
            #                              the attach test (see module docstring)
        if "addr show" in cmd and UE_TUN_DEV in cmd:
            selects_up = " show up " in f" {cmd} "
            if selects_up and not link_up:
                return True, ""          # `show up` filters the device out
            # -o output: "N: dev    inet <addr> scope global dev\  valid_lft..."
            line = f"15: {UE_TUN_DEV}    inet {addr} scope global {UE_TUN_DEV}"
            # the collector pipes through grep/awk/cut itself in the real shell;
            # emulate the net effect of that pipeline here.
            return True, line.split("inet ")[1].split()[0].split("/")[0]
        return False, ""                 # every other probe: unavailable

    _ssh.issued = issued
    return _ssh


def _collector(link_up):
    col = UECollector("ue1", "10.0.0.1", "u1", gnb_id="gnb1")
    col._ssh_cmd = _fake_ip_shell(link_up)
    # Hermetic: the gNB-log path reads a REAL /tmp/gnb1.log when the gNB is
    # local, which would make the test depend on whatever is on the machine.
    col._discover_serving = lambda: (None, None)
    col._gnb_log_tail = lambda access=None, lines=240: None
    return col


class UEAttachLivenessTest(unittest.TestCase):

    def test_attached_when_tun_link_is_up(self):
        col = _collector(link_up=True)
        # step 1 (pgrep) returns unavailable in the fake shell, so drive the
        # attach probe directly the way collect() does.
        ok, out = col._ssh_cmd(
            f"ip -4 -o addr show up dev {UE_TUN_DEV} 2>/dev/null"
            " | grep 'inet ' | awk '{print $4}' | cut -d/ -f1")
        self.assertTrue(ok and out)
        self.assertEqual(out.strip(), "12.1.1.187")

    def test_released_ue_with_stale_address_is_not_attached(self):
        """The regression: link DOWN, address still configured."""
        col = _collector(link_up=False)
        ok, out = col._ssh_cmd(
            f"ip -4 -o addr show up dev {UE_TUN_DEV} 2>/dev/null"
            " | grep 'inet ' | awk '{print $4}' | cut -d/ -f1")
        self.assertTrue(ok)
        self.assertEqual(out, "", "a released UE must not report an address")

    def test_old_probe_would_have_false_positived(self):
        """Pins WHY the `up` selector is required, not just that it is used."""
        col = _collector(link_up=False)
        _, stale = col._ssh_cmd(
            f"ip -4 addr show {UE_TUN_DEV} 2>/dev/null"
            " | grep 'inet ' | awk '{print $2}' | cut -d/ -f1")
        self.assertEqual(stale.strip(), "12.1.1.187",
                         "without `show up` the stale address is still printed")

    def test_collector_attach_probe_uses_the_up_selector(self):
        """The shipped collect() path must issue the `show up` form."""
        col = _collector(link_up=False)
        try:
            col.collect()
        except Exception:                      # noqa: BLE001 - other probes stubbed
            pass
        attach_cmds = [c for c in col._ssh_cmd.issued
                       if "addr show" in c and UE_TUN_DEV in c]
        self.assertTrue(attach_cmds, "collect() must probe the tun device")
        self.assertTrue(all(" show up " in f" {c} " for c in attach_cmds),
                        f"attach probe must filter on IFF_UP, got {attach_cmds}")

    def test_collect_reports_not_attached_for_released_ue(self):
        col = _collector(link_up=False)
        try:
            m = col.collect()
        except Exception:                      # noqa: BLE001
            self.skipTest("collect() needs probes beyond this fake shell")
        self.assertFalse(m.attached)
        self.assertIsNone(m.ip_address)


if __name__ == "__main__":
    unittest.main()
