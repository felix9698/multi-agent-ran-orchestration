"""P6: emulation mode - SimTelnetGNB command contract, SimExecutor real-path
round trips, ChannelModel action/environment separation, the deterministic
mock LLM backend, and end-to-end same-seed determinism.
"""

import tempfile
import unittest

from decision.llm_backend import DeterministicMockBackend, LLMBackendManager
from experiments.emulation import (
    ChannelModel, SimCollector, SimExecutor, SimTelnetGNB,
    build_emulated_coordinator, EMU_UE_RNTI,
)


def _make_executor(fail_axes=None):
    sims = {"gnb1": SimTelnetGNB(pci=0, fail_axes=fail_axes),
            "gnb2": SimTelnetGNB(pci=1)}
    sims["gnb1"].add_ue(0x4601)
    sims["gnb2"].add_ue(0x4602)
    return SimExecutor(sims), sims


class SimTelnetGNBTest(unittest.TestCase):
    """Responses must match the patch's printf formats (P7 readback)."""

    def setUp(self):
        self.gnb = SimTelnetGNB(pci=0)
        self.gnb.add_ue(0x4601)

    def test_rfatt_set_and_query(self):
        self.assertEqual(self.gnb.exec_cmd("ci rfatt 9.0"),
                         "TX attenuation set to 9.0 dB")
        self.assertIn("attenuation 9.0", self.gnb.exec_cmd("ci rfatt"))

    def test_prbcap_cell_and_per_ue(self):
        self.assertEqual(self.gnb.exec_cmd("ci prbcap 50"),
                         "DL PRB cap set to 50")
        self.assertEqual(self.gnb.exec_cmd("ci prbcap 24 4601"),
                         "UE 4601 DL PRB cap set to 24")
        listing = self.gnb.exec_cmd("ci prbcap")
        self.assertIn("DL PRB cap 50", listing)
        self.assertIn("UE 4601 DL PRB cap 24", listing)
        self.assertEqual(self.gnb.exec_cmd("ci prbcap 0"),
                         "DL PRB cap set to 0 (uncapped)")
        # patch fidelity: an uncapped UE contributes NO per-UE row
        self.gnb.exec_cmd("ci prbcap 0 4601")
        self.assertNotIn("UE 4601", self.gnb.exec_cmd("ci prbcap"))

    def test_sched_prio_forms(self):
        # single UE: no-RNTI form allowed
        self.assertEqual(self.gnb.exec_cmd("ci sched_prio 2.000"),
                         "UE 4601 PF weight set to 2.000")
        self.assertIn("UE 4601 PF weight 2.000",
                      self.gnb.exec_cmd("ci sched_prio"))
        # two UEs: no-RNTI form must fail (patch semantics)
        self.gnb.add_ue(0x4602)
        self.assertIn("error", self.gnb.exec_cmd("ci sched_prio 1.5"))
        self.assertEqual(self.gnb.exec_cmd("ci sched_prio 1.500 4602"),
                         "UE 4602 PF weight set to 1.500")

    def test_mcs_set_and_query(self):
        self.assertEqual(self.gnb.exec_cmd("ci mcs 10"),
                         "DL MCS cap set to [0..10]")
        self.assertIn("DL MCS cap [0..10]", self.gnb.exec_cmd("ci mcs"))

    def test_rnti_and_reestab(self):
        self.assertIn("RNTI 4601", self.gnb.exec_cmd("ci get_single_rnti"))
        self.assertIn("reestab count 0",
                      self.gnb.exec_cmd("ci get_reestab_count"))
        self.gnb.reestablish(0x4601, 0x9b3e)
        self.assertIn("reestab count 1",
                      self.gnb.exec_cmd("ci get_reestab_count"))
        self.assertIn("RNTI 9b3e", self.gnb.exec_cmd("ci get_single_rnti"))

    def test_fault_injection(self):
        gnb = SimTelnetGNB(fail_axes={"prbcap"})
        out = gnb.exec_cmd("ci prbcap 50")
        self.assertIn("error", out)
        self.assertNotIn("set to", out)

    def test_unknown_rnti_rejected(self):
        out = self.gnb.exec_cmd("ci prbcap 24 9999")
        self.assertIn("error", out)


class SimExecutorRoundTripTest(unittest.TestCase):
    """The REAL executor code paths against the emulated device."""

    def test_apply_axis_updates_mirror_and_device(self):
        ex, sims = _make_executor()
        self.assertTrue(ex.apply_axis("gnb1", "power_offset", 3.0))
        self.assertAlmostEqual(ex.states["gnb1"].power_offset_db, 3.0)
        self.assertAlmostEqual(sims["gnb1"].att_db, 9.0)   # 12 - 3
        self.assertTrue(ex.apply_axis("gnb1", "prb", 50))
        self.assertEqual(sims["gnb1"].dl_prb_cap, 50)
        self.assertTrue(ex.apply_axis("gnb1", "prb", 24, rnti=0x4601))
        self.assertEqual(sims["gnb1"].ues[0x4601]["prb_cap"], 24)
        self.assertTrue(ex.apply_axis("gnb1", "mcs_offset", -10))
        self.assertEqual(sims["gnb1"].mcs_max, 18)

    def test_apply_failure_reported(self):
        ex, _ = _make_executor(fail_axes={"prbcap"})
        self.assertFalse(ex.apply_axis("gnb1", "prb", 50))
        self.assertEqual(ex.states["gnb1"].prb_cap, 0)   # mirror untouched

    def test_snapshot_restore_round_trip(self):
        ex, sims = _make_executor()
        snap = ex.snapshot()
        ex.apply_axis("gnb1", "power_offset", 5.0)
        ex.apply_axis("gnb1", "prb", 40)
        ex.apply_axis("gnb1", "sched_priority", 2.0, rnti=0x4601)
        self.assertTrue(ex.restore(snap))
        self.assertAlmostEqual(ex.states["gnb1"].power_offset_db, 0.0)
        self.assertEqual(ex.states["gnb1"].prb_cap, 0)
        self.assertAlmostEqual(sims["gnb1"].att_db, 12.0)
        self.assertEqual(sims["gnb1"].dl_prb_cap, 0)
        self.assertAlmostEqual(sims["gnb1"].ues[0x4601]["pf_weight"], 1.0)

    def test_get_connected_rnti_through_sim(self):
        ex, _ = _make_executor()
        self.assertEqual(ex.get_connected_rnti("gnb1"), 0x4601)


class ChannelModelTest(unittest.TestCase):

    def test_environment_separate_from_action(self):
        ch = ChannelModel(seed=1, sigma=0.0)
        ch.set_environment(-5.0, 2.0)
        base = ch.throughput("gnb1", "gnb2", 0.0, 0.0)
        # +6 dB ACTION on the serving cell: environment unchanged
        boosted = ch.throughput("gnb1", "gnb2", 6.0, 0.0)
        self.assertAlmostEqual(boosted - base, 0.6 * 6.0, places=6)
        self.assertEqual(ch.env_gain["gnb1"], -5.0)   # env is its own input

    def test_neighbor_interference_penalty(self):
        ch = ChannelModel(seed=1, sigma=0.0)
        quiet = ch.throughput("gnb1", "gnb2", 0.0, 0.0)
        noisy = ch.throughput("gnb1", "gnb2", 0.0, 4.0)
        self.assertAlmostEqual(quiet - noisy, 0.3 * 4.0, places=6)

    def test_phase_script_crosses_target_both_directions(self):
        # constrained UE (gnb1 serving): Nominal above 8, Impairment below
        ch = ChannelModel(seed=1, sigma=0.0)
        ch.set_environment(3, 0)
        self.assertGreater(ch.throughput("gnb1", "gnb2", 0, 0), 8.0)
        ch.set_environment(-5, 2)
        self.assertLess(ch.throughput("gnb1", "gnb2", 0, 0), 8.0)

    def test_prb_and_sched_terms(self):
        ch = ChannelModel(seed=1, sigma=0.0)
        full = ch.throughput("gnb1", "gnb2", 0, 0)
        capped = ch.throughput("gnb1", "gnb2", 0, 0, prb_cap=56)
        self.assertLess(capped, full)
        favored = ch.throughput("gnb1", "gnb2", 0, 0, pf_weight=2.0)
        self.assertGreater(favored, full)

    def test_fit_is_placeholder(self):
        with self.assertRaises(NotImplementedError):
            ChannelModel().fit("grid.jsonl")


class DeterministicMockBackendTest(unittest.TestCase):

    FEAS_PROMPT = """=== ACTIVE INTENTS ===

=== NEW INTENT (to be evaluated) ===
{"id": "x", "target": {"kpi_name": "throughput", "constraint_type": "min",
 "target_value": 8.0, "unit": "Mbps"}, "scope": {"ue_ids": [], "bs_ids": []}}

=== CURRENT NETWORK STATE ===
{"ue_states": {"ue1": {"throughput_mbps": 4.9, "serving_bs": "bs1"},
               "ue2": {"throughput_mbps": 8.2, "serving_bs": "bs2"}}}

=== TASK ===
Analyze whether the new intent can be satisfied."""

    def test_parse_intent_contract(self):
        mock = DeterministicMockBackend(seed=1)
        r = mock.generate('Parse this network intent into structured form:\n'
                          '"Ensure UE downlink throughput >= 8 Mbps"')
        self.assertTrue(r.success)
        self.assertEqual(r.parsed_json["type"], "throughput_goal")
        self.assertEqual(r.parsed_json["value"], 8.0)
        self.assertEqual(r.parsed_json["constraint"], "min")

    def test_feasibility_contract(self):
        mock = DeterministicMockBackend(seed=1)
        r = mock.generate(self.FEAS_PROMPT)
        data = r.parsed_json
        for key in ("feasible", "confidence", "reasoning", "proposed_config",
                    "alternatives"):
            self.assertIn(key, data)
        self.assertGreaterEqual(data["confidence"], 0.05)
        self.assertLessEqual(data["confidence"], 0.99)
        self.assertEqual(len(data["alternatives"]), 2)
        for alt in data["alternatives"]:
            self.assertIn("target_value", alt)
        # bounded actions
        for key, val in data["proposed_config"].items():
            self.assertLessEqual(abs(val), 10.0)

    def test_alternatives_monotone_vs_rejected(self):
        mock = DeterministicMockBackend(seed=1)
        prompt = ('=== FAILED INTENT ===\n'
                  '{"target": {"target_value": 8.0}, '
                  '"rejected_alternatives": [{"id": "a", "target_value": 6.0}]}'
                  '\n=== ACTIVE INTENTS (must be preserved) ===\n[]')
        data = mock.generate(prompt).parsed_json
        for alt in data["alternatives"]:
            self.assertLess(alt["target_value"], 6.0)

    def test_same_seed_same_sequence(self):
        a = DeterministicMockBackend(seed=42)
        b = DeterministicMockBackend(seed=42)
        for _ in range(6):
            self.assertEqual(a.generate(self.FEAS_PROMPT).content,
                             b.generate(self.FEAS_PROMPT).content)

    def test_manager_registration_not_default(self):
        mgr = LLMBackendManager()
        self.assertIn("mock:deterministic", mgr.get_available_names())
        self.assertNotEqual(mgr.active_backend_name(), "mock:deterministic")
        self.assertTrue(mgr.set_backend("mock:deterministic"))
        self.assertEqual(mgr.active_backend_name(), "mock:deterministic")
        mgr.shutdown()


class SimCollectorTest(unittest.TestCase):

    def test_collect_all_returns_ue_metrics(self):
        ex, sims = _make_executor()
        ch = ChannelModel(seed=3, sigma=0.0)
        col = SimCollector(ch, ex)
        self.assertFalse(col.simulation_mode)
        metrics = col.collect_all()
        self.assertEqual(set(metrics), {"ue1", "ue2"})
        self.assertTrue(metrics["ue1"].attached)
        self.assertIsNotNone(metrics["ue1"].throughput_mbps)
        self.assertEqual(set(col.get_throughput_all(duration=1.0)),
                         {"ue1", "ue2"})

    def test_detached_ue_reports_unattached(self):
        ex, sims = _make_executor()
        col = SimCollector(ChannelModel(seed=3, sigma=0.0), ex)
        sims["gnb1"].detach_ue(EMU_UE_RNTI["ue1"])
        metrics = col.collect_all()
        self.assertFalse(metrics["ue1"].attached)
        self.assertIsNone(metrics["ue1"].throughput_mbps)
        self.assertTrue(metrics["ue2"].attached)


class EmulatedEndToEndDeterminismTest(unittest.TestCase):
    """Acceptance (b): same seed => identical deterministic outputs
    (wall-clock fields - durations, latency - excluded by design)."""

    def _mini_run(self, seed):
        from experiments.runner import ExperimentRunner
        from experiments.topology import two_ue_topology   # legacy 2-UE (explicit)
        coordinator, channel = build_emulated_coordinator(
            seed=seed, tau_trial_s=0.1, topology=two_ue_topology())
        try:
            with tempfile.TemporaryDirectory() as tmp:
                r = ExperimentRunner(coordinator=coordinator, output_dir=tmp,
                                     kpi_interval_s=0.0)
                r.channel_model = channel
                # experiment_seed is AUTHORITATIVE (set explicitly by direct
                # run_live callers; never inferred from backend private state)
                r.experiment_seed = seed
                steps, episodes = r.run_live(["llm_with_history"], trials=1)
        finally:
            coordinator.stop()
        S = [(s.phase, s.step_idx,
              tuple(sorted((u, round(k["throughput_mbps"], 3))
                           for u, k in s.ue_kpis.items())))
             for s in steps]
        def _r3(x):
            # throughput fields are Optional: an UNKNOWN/absent measurement stays
            # None (never a fabricated 0), so render it as None, not round(None).
            return None if x is None else round(x, 3)
        E = [(e.phase, e.trial_executed, e.trial_success, e.rolled_back,
              e.entered_negotiation, e.negotiation_rounds,
              round(e.confidence, 3), _r3(e.throughput_before),
              _r3(e.throughput_after),
              _r3(e.throughput_trial_min)) for e in episodes]
        return S, E

    def test_same_seed_identical_run(self):
        s1, e1 = self._mini_run(777)
        s2, e2 = self._mini_run(777)
        self.assertEqual(s1, s2)
        self.assertEqual(e1, e2)
        self.assertGreater(len(e1), 0)   # episodes actually happened

    def test_different_seed_differs(self):
        s1, _ = self._mini_run(777)
        s2, _ = self._mini_run(778)
        self.assertNotEqual(s1, s2)

    def test_emulated_action_space_is_106_prb(self):
        # H2: the emulated carrier is the 106-PRB profile (ChannelModel's
        # PRB term), stated explicitly - profile clamping from the default
        # 24-PRB config must not silently shrink the emulated action set
        from experiments.topology import two_ue_topology   # legacy 2-UE (explicit)
        coordinator, _ = build_emulated_coordinator(
            seed=1, topology=two_ue_topology())
        try:
            self.assertEqual(coordinator.action_space.prb_cap_max, 106)
            self.assertEqual(coordinator.action_space.ue_prb_cap_max, 106)
        finally:
            coordinator.stop()


if __name__ == "__main__":
    unittest.main()
