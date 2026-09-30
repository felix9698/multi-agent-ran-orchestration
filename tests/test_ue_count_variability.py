#!/usr/bin/env python3
"""UE-count variability: the UE count is NOT fixed at 3.

Current testbed fact = two provisioned UEs (ue1, ue2), both on gNB1. UE3+ are
LOGICAL EXPANSION SLOTS that auto-join once provisioned. These tests pin the
required behavior end to end:

  * Live-start gate: unprovisioned UEs are EXCLUDED (logged once), not fatal;
    the coordinator/GUI start path succeeds with >= 1 provisioned UE and fails
    closed ONLY when zero are provisioned. (0 / 1 / 2 / 3 provisioned cases.)
  * Every N-UE path (topology, collector, synthetic metrics, emulation) works
    for UE counts 1 / 2 / 3 / 4 with NO hardcoded count.
  * Emulation/synthetic DEFAULTS follow the provisioned real UE set.
  * Emulation RNTIs are derived (ue5, ue6, ... work) - no fixed table cap.
  * GUI per-UE colors cycle a palette for any UE count.

HARDWARE SAFETY: no test touches real hardware. The one live-start test stubs
the collector's collect_all() so the metrics thread performs NO SSH/iperf.
"""

import os
import unittest

from config import Config, NetworkConfig, GNBConfig, UEConfig


def _net(n, provisioned=None, serving="gnb1"):
    """A NetworkConfig with UEs ue1..ueN (all serving `serving` -> a shared cell
    for n>=2). `provisioned` is the set of ue ids given a full physical identity
    (default: all). Unprovisioned UEs have empty host/ip/ssh_user/imsi."""
    if provisioned is None:
        provisioned = {f"ue{i}" for i in range(1, n + 1)}
    net = NetworkConfig()
    net.gnbs = {
        "gnb1": GNBConfig(id="gnb1", hostname="h1", ip="10.0.0.1",
                          ssh_user="op1", pci=0, cell_id=1),
        "gnb2": GNBConfig(id="gnb2", hostname="h2", ip="10.0.0.2",
                          ssh_user="op2", pci=1, cell_id=2),
    }
    ues = {}
    for i in range(1, n + 1):
        uid = f"ue{i}"
        if uid in provisioned:
            ues[uid] = UEConfig(id=uid, hostname=f"10.1.0.{i}", ip=f"10.1.0.{i}",
                                ssh_user=f"op{i}", usrp_type="b206mini",
                                initial_serving_gnb=serving,
                                imsi=f"2089500000001{i:02d}")
        else:
            ues[uid] = UEConfig(id=uid, hostname="", ip="", ssh_user="",
                                usrp_type="b206mini",
                                initial_serving_gnb=serving, imsi="")
    net.ues = ues
    return net


class LiveStartGateTest(unittest.TestCase):
    """The urgent fix: an unprovisioned expansion slot must NOT block startup."""

    def test_zero_provisioned_fails_closed(self):
        cfg = Config(network=_net(2, provisioned=set()))
        with self.assertRaises(ValueError):
            cfg.validate_live_topology()

    def test_partition_reports_active_and_excluded(self):
        cfg = Config(network=_net(3, provisioned={"ue1", "ue2"}))
        active, excluded = cfg.live_ue_partition()
        self.assertEqual(active, ["ue1", "ue2"])
        self.assertEqual(set(excluded), {"ue3"})
        # validate returns the active set and does NOT raise (ue3 is a slot)
        self.assertEqual(cfg.validate_live_topology(), ["ue1", "ue2"])

    def _start_live(self, cfg):
        """Start a LIVE coordinator with the collector stubbed to a no-op so the
        metrics thread never touches real hardware. Returns the coordinator."""
        from coordinator.intent_coordinator import IntentCoordinator
        c = IntentCoordinator(config=cfg)
        # HARDWARE SAFETY: no real SSH/iperf from the background metrics loop.
        c.ue_collector.collect_all = lambda *a, **k: {}
        self.assertFalse(getattr(c.ue_collector, "simulation_mode", False))
        c.start()
        return c

    def test_one_provisioned_starts_and_trims(self):
        cfg = Config(network=_net(3, provisioned={"ue1"}))
        c = self._start_live(cfg)
        try:
            self.assertTrue(c.running)
            self.assertEqual(set(c.ue_serving_gnb), {"ue1"})
            self.assertEqual(set(c.ue_collector.get_ue_ids()), {"ue1"})
            self.assertEqual(set(c.llm_manager.ue_ids), {"ue1"})
        finally:
            c.stop()

    def test_two_provisioned_excludes_slot_and_logs_once(self):
        # The CURRENT real testbed: ue1+ue2 provisioned, ue3 an expansion slot.
        cfg = Config(network=_net(3, provisioned={"ue1", "ue2"}))
        from coordinator.intent_coordinator import IntentCoordinator
        c = IntentCoordinator(config=cfg)
        c.ue_collector.collect_all = lambda *a, **k: {}
        with self.assertLogs("IntentCoordinator", level="INFO") as cm:
            c.start()
        try:
            self.assertTrue(c.running)                       # started, not blocked
            self.assertEqual(set(c.ue_serving_gnb), {"ue1", "ue2"})
            self.assertNotIn("ue3", c.ue_collector.get_ue_ids())
            self.assertEqual(set(c.llm_manager.ue_ids), {"ue1", "ue2"})
            once = [l for l in cm.output if "ue3: not provisioned" in l]
            self.assertEqual(len(once), 1)                   # logged exactly once
            self.assertIn("expansion slot", once[0])
        finally:
            c.stop()

    def test_three_provisioned_keeps_all(self):
        cfg = Config(network=_net(3, provisioned={"ue1", "ue2", "ue3"}))
        c = self._start_live(cfg)
        try:
            self.assertTrue(c.running)
            self.assertEqual(set(c.ue_serving_gnb), {"ue1", "ue2", "ue3"})
            self.assertEqual(set(c.ue_collector.get_ue_ids()),
                             {"ue1", "ue2", "ue3"})
        finally:
            c.stop()

    def test_zero_provisioned_coordinator_start_raises(self):
        from coordinator.intent_coordinator import IntentCoordinator
        cfg = Config(network=_net(2, provisioned=set()))
        c = IntentCoordinator(config=cfg)
        c.ue_collector.collect_all = lambda *a, **k: {}
        with self.assertRaises(ValueError):
            c.start()
        self.assertFalse(getattr(c, "running", False))


class DefaultConfigStartsHeadlessTest(unittest.TestCase):
    """Reproduce the operator's exact failure on the SHIPPED default config: two
    provisioned UEs + an unprovisioned ue3 slot must start, not crash."""

    _ENV = ("LIVE_UE_SET", "UE3_HOST", "UE3_IP", "UE3_SSH_USER", "UE3_IMSI",
            "UE3_USRP_TYPE")

    def setUp(self):
        import config
        self.config = config
        self._saved = {k: os.environ.pop(k, None) for k in self._ENV}
        config._config = None

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        self.config._config = None

    def test_default_config_coordinator_starts_and_excludes_ue3(self):
        from coordinator.intent_coordinator import IntentCoordinator
        cfg = self.config.get_default_config()
        self.assertEqual(list(cfg.network.ues), ["ue1", "ue2", "ue3"])
        self.assertFalse(cfg.network.ues["ue3"].is_physically_provisioned())
        c = IntentCoordinator(config=cfg)
        c.ue_collector.collect_all = lambda *a, **k: {}   # no real SSH/iperf
        c.start()                                          # MUST NOT raise
        try:
            self.assertTrue(c.running)
            self.assertEqual(set(c.ue_serving_gnb), {"ue1", "ue2"})
            self.assertNotIn("ue3", c.ue_collector.get_ue_ids())
        finally:
            c.stop()


class ParameterizedUeCountTest(unittest.TestCase):
    """topology / collector / synthetic-metrics / emulation for 1/2/3/4 UEs."""

    COUNTS = (1, 2, 3, 4)

    def test_from_network_config_enumerates_n(self):
        from experiments.topology import from_network_config
        for n in self.COUNTS:
            with self.subTest(n=n):
                topo = from_network_config(_net(n))
                self.assertEqual(set(topo.ue_ids()),
                                 {f"ue{i}" for i in range(1, n + 1)})
                self.assertEqual(topo.n_ues(), n)

    def test_provisioned_emulation_topology_enumerates_n(self):
        from experiments.topology import provisioned_emulation_topology
        for n in self.COUNTS:
            with self.subTest(n=n):
                topo = provisioned_emulation_topology(_net(n))
                self.assertEqual(set(topo.ue_ids()),
                                 {f"ue{i}" for i in range(1, n + 1)})
                # every emulated UE carries a distinct RNTI within its cell
                rntis = [topo.rnti(u) for u in topo.ue_ids()]
                self.assertEqual(len(set(rntis)), n)

    def test_collector_from_config_enumerates_n(self):
        from collectors.multi_ue_collector import MultiUECollector
        for n in self.COUNTS:
            with self.subTest(n=n):
                col = MultiUECollector.from_config(Config(network=_net(n)),
                                                   simulation_mode=True)
                try:
                    self.assertEqual(set(col.get_ue_ids()),
                                     {f"ue{i}" for i in range(1, n + 1)})
                finally:
                    col.shutdown()

    def test_synthetic_metrics_path_enumerates_n(self):
        from experiments.synthetic import generate_experiment
        from experiments.metrics import compute_multi_method
        for n in self.COUNTS:
            with self.subTest(n=n):
                ids = tuple(f"ue{i}" for i in range(1, n + 1))
                steps, eps = generate_experiment(["llm_with_history"], trials=1,
                                                 ue_ids=ids, seed=7)
                seen = set()
                for s in steps:
                    seen.update(s.ue_kpis.keys())
                self.assertEqual(seen, set(ids))
                m = compute_multi_method(steps, eps)     # metrics must not crash
                self.assertIn("llm_with_history", m)

    def test_emulation_enumerates_n(self):
        from experiments.emulation import build_emulated_coordinator
        from experiments.topology import provisioned_emulation_topology
        for n in self.COUNTS:
            with self.subTest(n=n):
                topo = provisioned_emulation_topology(_net(n))
                c, _ch = build_emulated_coordinator(seed=1, tau_trial_s=0.1,
                                                    topology=topo)
                try:
                    self.assertEqual(set(c.ue_collector.collect_all()),
                                     {f"ue{i}" for i in range(1, n + 1)})
                finally:
                    c.stop()


class ProvisionedDefaultsTest(unittest.TestCase):
    """Emulation/synthetic DEFAULTS follow the PROVISIONED real UE set, and an
    unprovisioned slot is fail-closed OUT of the default."""

    def test_provisioned_topology_excludes_unprovisioned(self):
        from experiments.topology import provisioned_emulation_topology
        net = _net(3, provisioned={"ue1", "ue2"})   # ue3 is a slot
        topo = provisioned_emulation_topology(net)
        self.assertEqual(set(topo.ue_ids()), {"ue1", "ue2"})
        self.assertNotIn("ue3", topo.ue_serving)
        # both provisioned UEs share gNB1 -> a real 2-UE shared cell
        self.assertEqual(topo.primary_shared_cell(), "gnb1")
        self.assertEqual(set(topo.contending_ues()), {"ue1", "ue2"})

    def test_provisioned_topology_falls_back_when_none_provisioned(self):
        from experiments.topology import provisioned_emulation_topology
        topo = provisioned_emulation_topology(_net(2, provisioned=set()))
        # nothing provisioned -> the legacy pair fallback (never a hard failure)
        self.assertEqual(set(topo.ue_ids()), {"ue1", "ue2"})

    def test_synthetic_default_excludes_unprovisioned(self):
        # _primary_ue_ids() reads config; with the default config ue3 is an
        # unprovisioned slot, so the synthetic DEFAULT is ue1/ue2 only.
        import config as cfgmod
        from experiments.synthetic import _primary_ue_ids
        saved = cfgmod._config
        try:
            cfgmod.set_config(Config(network=_net(3, provisioned={"ue1", "ue2"})))
            self.assertEqual(set(_primary_ue_ids()), {"ue1", "ue2"})
            cfgmod.set_config(Config(network=_net(4)))   # all provisioned
            self.assertEqual(set(_primary_ue_ids()),
                             {"ue1", "ue2", "ue3", "ue4"})
        finally:
            cfgmod._config = saved


class DynamicEmulationRntiTest(unittest.TestCase):
    """Emulation RNTIs are DERIVED, not a fixed ue1..ue4 table (ue5+ work)."""

    def test_rnti_derivation_scales_beyond_table(self):
        from experiments.topology import emulation_rnti
        for i in range(1, 7):
            self.assertEqual(emulation_rnti(f"ue{i}"), 0x4600 + i)
        # distinct across a large set
        rntis = [emulation_rnti(f"ue{i}") for i in range(1, 9)]
        self.assertEqual(len(set(rntis)), 8)

    def test_add_adversarial_fifth_ue(self):
        from experiments.topology import (three_ue_shared_topology,
                                          add_adversarial_ue, emulation_rnti)
        t = add_adversarial_ue(three_ue_shared_topology(), "ue5")
        self.assertIn("ue5", t.ue_ids())
        self.assertEqual(t.rnti("ue5"), emulation_rnti("ue5"))
        self.assertEqual(t.rnti("ue5"), 0x4605)


class GuiColorPaletteTest(unittest.TestCase):
    """Per-UE colors cycle a palette -> distinct colors for ANY UE count."""

    def test_configured_ue_colors_scale(self):
        from gui.dashboard import configured_ue_colors
        colors = configured_ue_colors(Config(network=_net(5)))
        self.assertEqual(len(colors), 5)
        self.assertEqual(len(set(colors.values())), 5)   # all distinct


if __name__ == "__main__":
    unittest.main()
